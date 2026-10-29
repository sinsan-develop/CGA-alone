"""명시 승인한 개발 QA 전용 실행기. 일반 서비스 기동에서는 import/실행하지 않는다.

실제 JWT·기존 app/runtime·전용 cga_alone_qa DB를 사용한다. 모델 전송만 파일 영속
대역으로 고정하고, 정상/강제 재시작 뒤 같은 user/bot/room/provider ID를 복원한다.
실제 Provider 성공 증거가 아니다. 운영 DB·postgres 계정과 암묵적 실행은 거부한다.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
from threading import RLock
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.schemas.persistent_llm import TurnScope
from tests.cga_alone.test_persistent_llm_provider import CONFIG
from tests.cga_alone.test_persistent_llm_recovery import MemoryTransport


def validate_qa_url(candidate: str, production: str):
    parsed = make_url(candidate)
    if (
        parsed.database != "cga_alone_qa"
        or parsed.username != "cga_alone"
        or parsed == make_url(production)
    ):
        raise ValueError("전용 QA DB와 비관리자 전용 계정만 사용할 수 있습니다.")
    return candidate


def _private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class DurableQaTransport(MemoryTransport):
    """실제 원격 서비스처럼 API process 재시작과 독립적으로 대역 이력을 보존한다."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.lock = RLock()
        self.namespace = uuid4().hex
        self.conversations = {}

    def __call__(self, method, url, payload, headers, timeout):
        with self.lock:
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding="utf-8"))
                for key in (
                    "items",
                    "calls",
                    "generations",
                    "appends",
                    "namespace",
                    "conversations",
                ):
                    setattr(self, key, state[key])
            if method == "POST" and url.endswith("/conversations"):
                self.calls.append((method, url, deepcopy(payload)))
                identifier = (
                    "conv_" + self.namespace + "_" + str(len(self.conversations) + 1)
                )
                self.conversations[identifier] = []
                result = {"id": identifier}
            else:
                if "/conversations/" in url:
                    identifier = url.split("/conversations/", 1)[1].split("/", 1)[0]
                    self.items = self.conversations[identifier]
                result = super().__call__(method, url, payload, headers, timeout)
            _private_json(
                self.path,
                {
                    key: getattr(self, key)
                    for key in (
                        "items",
                        "calls",
                        "generations",
                        "appends",
                        "namespace",
                        "conversations",
                    )
                },
            )
            return result


def _context(engine, packet):
    values = {key: UUID(value) for key, value in packet["owner"].items()}
    scope = TurnScope(
        **{
            key: value if key in {"generation", "principal_key"} else UUID(value)
            for key, value in packet["scope"].items()
        }
    )
    return SimpleNamespace(engine=engine, scope=scope, **values)


def _assert_owned(db, ctx):
    from app.models import Bot, BotVersion, ChannelRoom, Group, Organization, User

    org = db.get(Organization, ctx.org_id)
    user = db.get(User, ctx.user_id)
    bot = db.get(Bot, ctx.bot_id)
    group = db.get(Group, ctx.group_id)
    version = db.get(BotVersion, ctx.version_id)
    room = db.get(ChannelRoom, ctx.room_id)
    if not (
        org
        and org.code.startswith("pllm-")
        and user
        and user.organization_id == org.id
        and user.login_id.startswith("pllm")
        and bot
        and bot.created_by == user.id
        and bot.organization_id == org.id
        and group is not None
        and group.organization_id == org.id
        and user.group_id == group.id
        and bot.group_id == group.id
        and version is not None
        and version.bot_id == bot.id
        and room is not None
        and room.bot_id == bot.id
    ):
        raise ValueError("이 실행기가 만든 합성 QA 소유 범위가 아닙니다.")


def _seed(engine, private_path):
    import pytest
    from app.core.security import get_password_hash, decode_access_token
    from app.models import User
    from tests.cga_alone.conftest import persistent_context
    from tests.cga_alone.test_persistent_llm_access import authenticated_http

    patch = pytest.MonkeyPatch()
    context_generator = persistent_context.__wrapped__(engine, None)
    ctx = next(context_generator)
    http_generator = authenticated_http.__wrapped__(ctx, patch)
    fixture = next(http_generator)
    password = secrets.token_urlsafe(24)
    with Session(engine) as db:
        user = db.get(User, ctx.user_id)
        user.password_hash = get_password_hash(password)
        login = user.login_id
        db.commit()
    packet = {
        "owner": {
            key: str(getattr(ctx, key))
            for key in (
                "org_id",
                "group_id",
                "user_id",
                "bot_id",
                "version_id",
                "room_id",
            )
        },
        "scope": asdict(ctx.scope),
        "license_id": str(fixture[5]),
        "other_user_id": decode_access_token(fixture[3]["Authorization"][7:])["sub"],
        "login_id": login,
        "password": password,
        "policy": {
            name: getattr(settings, name)
            for name in type(settings).model_fields
            if name.startswith("persistent_llm_")
        },
    }
    packet["scope"] = {
        key: value if key in {"generation", "principal_key"} else str(value)
        for key, value in packet["scope"].items()
    }
    _private_json(private_path, packet)
    # keep-on-stop의 os._exit 전까지 generator의 정상정리 finally가 실행되지 않도록 보존한다.
    return ctx, packet, (context_generator, http_generator, patch)


def _configure(ctx, packet, state_path):
    import importlib
    from app.api.deps import get_db
    from app.api.routes import bots, channels
    from app.main import app
    from app.services.llm_client import LlmChatClient

    sessions = sessionmaker(bind=ctx.engine, autoflush=False)
    for module in (
        channels,
        importlib.import_module("app.main"),
        importlib.import_module("app.api.deps"),
    ):
        module.SessionLocal = sessions

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    for name, value in packet["policy"].items():
        if not name.startswith("persistent_llm_"):
            raise ValueError("QA 정책 범위 오류")
        setattr(settings, name, value)
    assert settings.persistent_llm_sample_bot_id == str(ctx.bot_id)
    assert settings.persistent_llm_tariff_model == CONFIG.model
    os.environ["AIDOT_CHATGPT_API_KEY"] = CONFIG.api_key
    os.environ["AIDOT_CHATGPT_BASE_URL"] = CONFIG.base_url
    transport = DurableQaTransport(state_path)
    bots._persistent_provider_transport = lambda: transport

    def forbidden(*args, **kwargs):
        raise AssertionError("QA에서는 기존 단발/NLU/외부 모델 호출이 금지됩니다.")

    LlmChatClient.chat = forbidden
    return app


def _cleanup(ctx, packet):
    from app.models import (
        AuditLog,
        Bot,
        BotVersion,
        ChannelLlmBinding,
        ChannelLlmVersionGuard,
        ChannelMessage,
        ChannelQueueEvent,
        ChannelRoom,
        LlmBudgetAccount,
        LlmBudgetReservation,
        VersionDialogAsset,
        VersionDialogFlowGraph,
        User,
        Group,
        Organization,
        AdminLicense,
        EditLock,
        UserRole,
    )

    with Session(ctx.engine) as db:
        _assert_owned(db, ctx)
        other = db.get(User, UUID(packet["other_user_id"]))
        license = db.get(AdminLicense, UUID(packet["license_id"]))
        assert (
            other is not None
            and other.organization_id == ctx.org_id
            and other.login_id.startswith("pllm-other-")
        )
        assert license is not None and license.organization_id == ctx.org_id
        rooms = list(
            db.scalars(select(ChannelRoom.id).where(ChannelRoom.bot_id == ctx.bot_id))
        )
        versions = list(
            db.scalars(select(BotVersion.id).where(BotVersion.bot_id == ctx.bot_id))
        )
        events = list(
            db.scalars(
                select(ChannelQueueEvent.id).where(
                    ChannelQueueEvent.bot_id == ctx.bot_id
                )
            )
        )
        accounts = list(
            db.scalars(
                select(LlmBudgetAccount.id).where(
                    LlmBudgetAccount.scope_key.contains(str(ctx.org_id))
                )
            )
        )
        db.execute(
            update(Bot).where(Bot.id == ctx.bot_id).values(active_version_id=None)
        )
        db.execute(
            delete(LlmBudgetReservation).where(
                LlmBudgetReservation.event_id.in_(events)
            )
        )
        db.execute(delete(ChannelQueueEvent).where(ChannelQueueEvent.id.in_(events)))
        db.execute(delete(ChannelMessage).where(ChannelMessage.room_id.in_(rooms)))
        db.execute(
            delete(ChannelLlmBinding).where(ChannelLlmBinding.bot_id == ctx.bot_id)
        )
        db.execute(
            delete(ChannelLlmVersionGuard).where(
                ChannelLlmVersionGuard.version_id.in_(versions)
            )
        )
        db.execute(delete(LlmBudgetAccount).where(LlmBudgetAccount.id.in_(accounts)))
        db.execute(delete(EditLock).where(EditLock.bot_id == ctx.bot_id))
        db.execute(delete(ChannelRoom).where(ChannelRoom.id.in_(rooms)))
        db.execute(
            delete(VersionDialogFlowGraph).where(
                VersionDialogFlowGraph.version_id.in_(versions)
            )
        )
        db.execute(
            delete(VersionDialogAsset).where(
                VersionDialogAsset.version_id.in_(versions)
            )
        )
        db.execute(delete(BotVersion).where(BotVersion.id.in_(versions)))
        db.execute(delete(Bot).where(Bot.id == ctx.bot_id))
        db.execute(
            delete(AuditLog).where(
                (AuditLog.actor_user_id.in_([ctx.user_id, other.id]))
                | AuditLog.target_id.in_([ctx.bot_id, *rooms, *versions, *events])
            )
        )
        db.execute(
            delete(UserRole).where(UserRole.user_id.in_([ctx.user_id, other.id]))
        )
        db.execute(delete(AdminLicense).where(AdminLicense.id == license.id))
        db.execute(delete(User).where(User.id.in_([ctx.user_id, other.id])))
        db.execute(delete(Group).where(Group.id == ctx.group_id))
        db.execute(delete(Organization).where(Organization.id == ctx.org_id))
        db.commit()
    from app.core.config import ROOT_DIR

    (ROOT_DIR / ".local/pllm-fixture-owners" / (str(ctx.org_id) + ".json")).unlink(
        missing_ok=True
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "resume", "cleanup"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--keep-on-stop", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CGA_PERSISTENT_QA") != "1":
        raise ValueError("명시적 QA 실행 허용이 필요합니다.")
    url = validate_qa_url(
        os.environ.get("AIDOT_TEST_DATABASE_URL", ""), settings.sqlalchemy_database_url
    )
    engine = create_engine(url, pool_pre_ping=True)
    directory = args.state_dir.resolve()
    private = directory / "owner-private.json"
    generators = None
    if args.action == "seed":
        if private.exists():
            raise ValueError("기존 QA 소유 기록은 덮어쓰지 않습니다.")
        ctx, packet, generators = _seed(engine, private)
    else:
        packet = json.loads(private.read_text(encoding="utf-8"))
        ctx = _context(engine, packet)
        with Session(engine) as db:
            _assert_owned(db, ctx)
    if args.action == "cleanup":
        _cleanup(ctx, packet)
        _private_json(
            directory / "cleanup.json",
            {"owner": packet["owner"], "cleaned": True, "actual_openai_calls": 0},
        )
        engine.dispose()
        return
    app = _configure(ctx, packet, directory / "provider-state.json")
    import uvicorn

    exit_code = 0
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except BaseException as error:
        exit_code = 1
        _private_json(
            directory / "startup-failure.json",
            {"failed": True, "error_type": type(error).__name__},
        )
        raise
    finally:
        if args.keep_on_stop:
            # 대역 원격파일과 QA DB를 명시적으로 보존해 다음 API process에서 같은 맥락을 복원한다.
            os._exit(exit_code)
        if generators:
            context_generator, http_generator, patch = generators
            for generator in (http_generator, context_generator):
                try:
                    next(generator)
                except StopIteration:
                    pass
            patch.undo()
        else:
            _cleanup(ctx, packet)
        engine.dispose()


if __name__ == "__main__":
    main()
