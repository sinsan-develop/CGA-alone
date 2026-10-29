"""등록한 샘플 버전의 열린 참조·쓰기 장벽. 긴 작업 중 transaction은 호출자가 분리한다."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib, json
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import (
    Bot,
    BotVersion,
    ChannelRoom,
    User,
    ChannelQueueEvent,
    ChannelLlmBinding,
    ChannelLlmVersionGuard,
)
from app.schemas.persistent_llm import TurnScope, MutationToken


class PinConflict(ValueError):
    pass


def _lock_version(db: Session, version_id: UUID, *, include_deleted=False):
    query = select(BotVersion).where(BotVersion.id == version_id)
    if not include_deleted:
        query = query.where(BotVersion.deleted_at.is_(None))
    version = db.scalar(query.with_for_update())
    if version is None:
        raise PinConflict("버전을 찾을 수 없습니다.")
    return version


def register_sample_version(db: Session, version_id: UUID):
    """첫 등록 전 기존 writer 종료·manifest 입증은 활성화 절차에서 확인해야 한다."""
    _lock_version(db, version_id)
    guard = db.get(ChannelLlmVersionGuard, version_id, populate_existing=True)
    if guard is None:
        guard = ChannelLlmVersionGuard(
            version_id=version_id, mutation_status="idle", fence=0
        )
        db.add(guard)
        db.flush()
    return guard


def open_pinned_dialog(
    db: Session, scope: TurnScope, snapshot: dict
) -> ChannelLlmBinding:
    version = _lock_version(db, scope.version_id)
    guard = db.get(ChannelLlmVersionGuard, scope.version_id, populate_existing=True)
    if guard is None or guard.mutation_status != "idle":
        raise PinConflict("샘플 버전 준비 또는 쓰기 종료 확인이 필요합니다.")
    bot = db.get(Bot, scope.bot_id)
    try:
        if not scope.principal_key.startswith("user:"):
            raise ValueError()
        user_id = UUID(scope.principal_key[5:])
    except (ValueError, TypeError):
        raise PinConflict("인증된 주체가 필요합니다.") from None
    user = db.get(User, user_id)
    if (
        bot is None
        or bot.deleted_at is not None
        or version.bot_id != scope.bot_id
        or bot.organization_id != scope.org_id
        or bot.group_id != scope.group_id
        or bot.created_by != user_id
        or user is None
        or user.deleted_at is not None
        or user.organization_id != scope.org_id
        or user.group_id != scope.group_id
    ):
        raise PinConflict("샘플 대화의 인증 범위가 일치하지 않습니다.")
    ai_config = dict(bot.data_json or {})
    system_config = getattr(version, "system_config_json", None)
    if not isinstance(system_config, dict):
        system_config = (version.version_json or {}).get("system_config", {})
    if isinstance(system_config.get("ai_config"), dict):
        ai_config.update(system_config["ai_config"])
    source_kind = (
        (snapshot.get("knowledge") or {}).get("kind")
        if isinstance(snapshot.get("knowledge"), dict)
        else None
    )
    if ai_config.get("answer_mode") in {"semantic_rag", "llm_rag"} or source_kind in {
        "external_rag",
        "wiki",
        "mutable_vector",
    }:
        raise PinConflict(
            "공유 검색 지식의 불변성을 검증하는 어댑터가 준비되기 전에는 지속 대화를 활성화할 수 없습니다."
        )
    room = db.scalar(
        select(ChannelRoom).where(ChannelRoom.id == scope.room_id).with_for_update()
    )
    if (
        room is None
        or room.deleted_at is not None
        or room.bot_id != scope.bot_id
        or room.bot_version_id != scope.version_id
        or room.status != "open"
        or scope.generation < 1
    ):
        raise PinConflict("방 범위가 일치하지 않습니다.")
    live = db.scalar(
        select(ChannelLlmBinding)
        .where(
            ChannelLlmBinding.room_id == scope.room_id,
            ChannelLlmBinding.principal_key == scope.principal_key,
            ChannelLlmBinding.status != "closed",
        )
        .with_for_update()
    )
    if live is not None:
        if live.generation != scope.generation or live.version_id != scope.version_id:
            raise PinConflict("기존 대화를 명시적으로 종료해야 합니다.")
        return live
    if db.scalar(
        select(ChannelLlmBinding.id).where(
            ChannelLlmBinding.room_id == scope.room_id,
            ChannelLlmBinding.principal_key == scope.principal_key,
            ChannelLlmBinding.generation >= scope.generation,
        )
    ):
        raise PinConflict("대화 generation을 재사용할 수 없습니다.")
    frozen = deepcopy(snapshot)
    digest = hashlib.sha256(
        json.dumps(
            frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    binding = ChannelLlmBinding(
        organization_id=scope.org_id,
        group_id=scope.group_id,
        bot_id=scope.bot_id,
        version_id=scope.version_id,
        room_id=scope.room_id,
        user_id=user_id,
        principal_key=scope.principal_key,
        generation=scope.generation,
        status="open",
        snapshot_json=frozen,
        snapshot_hash=digest,
    )
    db.add(binding)
    db.flush()
    return binding


def close_pinned_dialog(db: Session, binding_id: UUID):
    existing = db.get(ChannelLlmBinding, binding_id)
    if existing is None:
        raise PinConflict("대화가 없습니다.")
    _lock_version(db, existing.version_id, include_deleted=True)
    db.scalar(
        select(ChannelRoom).where(ChannelRoom.id == existing.room_id).with_for_update()
    )
    binding = db.scalar(
        select(ChannelLlmBinding)
        .where(ChannelLlmBinding.id == binding_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if binding.status == "closed":
        return binding
    pending = db.scalar(
        select(ChannelQueueEvent.id)
        .where(
            ChannelQueueEvent.binding_id == binding_id,
            (ChannelQueueEvent.status.in_(("queued", "processing", "unknown")))
            | (
                ChannelQueueEvent.phase.in_(
                    (
                        "submitting",
                        "submitted",
                        "unknown",
                        "pending_delivery",
                        "memory_syncing",
                        "needs_reconcile",
                    )
                )
            ),
        )
        .limit(1)
    )
    if pending:
        raise PinConflict("진행/결과불명 턴을 먼저 정리해야 합니다.")
    binding.status = "closed"
    binding.closed_at = datetime.now(timezone.utc)
    db.flush()
    return binding


def begin_version_mutation(
    db: Session, version_id: UUID, operation: str
) -> MutationToken | None:
    if db.get(ChannelLlmVersionGuard, version_id, populate_existing=True) is None:
        return None
    _lock_version(db, version_id)
    guard = db.get(ChannelLlmVersionGuard, version_id, populate_existing=True)
    if guard is None:
        return None
    if guard.mutation_status != "idle":
        raise PinConflict("이전 쓰기의 종료 확인이 필요합니다.")
    if db.scalar(
        select(ChannelLlmBinding.id)
        .where(
            ChannelLlmBinding.version_id == version_id,
            ChannelLlmBinding.status != "closed",
        )
        .limit(1)
    ):
        raise PinConflict("열린 지속 대화가 이 버전을 사용 중입니다.")
    guard.fence += 1
    guard.mutation_token = uuid4()
    guard.mutation_status = "running"
    guard.operation = operation
    guard.heartbeat_at = datetime.now(timezone.utc)
    db.flush()
    return MutationToken(version_id, guard.mutation_token, guard.fence)


def assert_version_mutation_token(db: Session, token: MutationToken):
    _lock_version(db, token.version_id, include_deleted=True)
    guard = db.get(ChannelLlmVersionGuard, token.version_id, populate_existing=True)
    if (
        guard is None
        or guard.mutation_token != token.token
        or guard.fence != token.fence
        or guard.mutation_status != "running"
    ):
        raise PinConflict("쓰기 token/fence가 유효하지 않습니다.")
    return guard


def finish_version_mutation(db: Session, token: MutationToken, manifest_hash: str):
    guard = assert_version_mutation_token(db, token)
    if len(manifest_hash) != 64 or any(
        ch not in "0123456789abcdef" for ch in manifest_hash
    ):
        raise PinConflict("manifest 검증값이 필요합니다.")
    guard.manifest_hash = manifest_hash
    guard.mutation_status = "idle"
    guard.mutation_token = None
    guard.owner = None
    guard.heartbeat_at = datetime.now(timezone.utc)
    db.flush()


# 공개 함수의 기존 인자/응답을 보존하는 admission 문맥. provider 대화 이력과는 관계없다.
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
from sqlalchemy.orm import object_session
from fastapi import HTTPException


@dataclass
class _WriteContext:
    operation: str
    db: Session | None = None
    tokens: dict = field(default_factory=dict)
    external_started: set = field(default_factory=set)
    transferred: dict = field(default_factory=dict)
    removed_guards: set = field(default_factory=set)


_active_write = ContextVar("persistent_version_write", default=None)


def _completion_hash(db, version_id):
    version = db.get(BotVersion, version_id)
    # 이것은 완료된 DB 쓰기의 지문이며 외부 지식 불변의 증거로 사용하지 않는다.
    return hashlib.sha256(
        json.dumps(
            {"kind": "version_document_receipt", "document": version.version_json},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def admit_version_write(db: Session, version: BotVersion, *, force=False):
    context = _active_write.get()
    if context is None or (context.operation == "copy" and not force):
        return None
    # 영속 행에만 lease가 존재한다. 기존 독립적인 document serializer/임시 객체는 대상이 아니다.
    if not isinstance(version, BotVersion) or object_session(version) is not db:
        return None
    if version.id in context.tokens:
        return context.tokens[version.id]
    token = begin_version_mutation(db, version.id, context.operation)
    if token is not None:
        context.db = db
        context.tokens[version.id] = token
        db.get(ChannelLlmVersionGuard, version.id).owner = "request:" + uuid4().hex
        db.commit()  # 외부 작업에 앞서 durable lease만 보존하고 행 장벽은 놓는다.
    return token


def admit_bot_version_writes(db: Session, bot: Bot):
    context = _active_write.get()
    if context is None or not isinstance(bot, Bot) or object_session(bot) is not db:
        return
    ids = db.scalars(
        select(BotVersion.id)
        .join(
            ChannelLlmVersionGuard, ChannelLlmVersionGuard.version_id == BotVersion.id
        )
        .where(BotVersion.bot_id == bot.id)
        .order_by(BotVersion.id)
    ).all()
    for version_id in ids:
        admit_version_write(db, db.get(BotVersion, version_id), force=True)


def assert_active_version_write(version: BotVersion):
    context = _active_write.get()
    if context is not None and version.id in context.tokens:
        assert_version_mutation_token(context.db, context.tokens[version.id])


def mark_external_version_write(version_id):
    context = _active_write.get()
    if context is None or version_id not in context.tokens:
        return
    assert_version_mutation_token(context.db, context.tokens[version_id])
    context.db.commit()
    context.external_started.add(version_id)


def handoff_version_write(db: Session, version_id, job_id):
    context = _active_write.get()
    if context is None or version_id not in context.tokens:
        return
    guard = assert_version_mutation_token(db, context.tokens[version_id])
    guard.owner = "queue:" + str(job_id)
    context.transferred[version_id] = job_id
    db.flush()


def _finish_context(context, failed):
    if context.db is None or not context.tokens:
        return
    db = context.db
    if failed:
        db.rollback()
    try:
        for version_id in sorted(context.tokens, key=str):
            job_id = context.transferred.get(version_id)
            if job_id is not None and db.get(ChannelQueueEvent, job_id) is not None:
                continue
            token = context.tokens[version_id]
            if (
                version_id in context.removed_guards
                and db.get(BotVersion, version_id) is None
                and db.get(ChannelLlmVersionGuard, version_id) is None
            ):
                continue
            guard = assert_version_mutation_token(db, token)
            if failed and version_id in context.external_started:
                guard.mutation_status = "unknown"
                guard.heartbeat_at = datetime.now(timezone.utc)
            else:
                finish_version_mutation(db, token, _completion_hash(db, version_id))
        db.commit()
    except Exception:
        db.rollback()
        raise


@contextmanager
def _write_scope(operation):
    if _active_write.get() is not None:
        yield _active_write.get()
        return
    context = _WriteContext(operation)
    handle = _active_write.set(context)
    try:
        try:
            yield context
        except Exception:
            _finish_context(context, True)
            raise
        else:
            _finish_context(context, False)
    finally:
        _active_write.reset(handle)


def version_mutation(operation):
    def decorate(function):
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def wrapped(*args, **kwargs):
                try:
                    with _write_scope(operation):
                        return await function(*args, **kwargs)
                except PinConflict as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error

        else:

            @wraps(function)
            def wrapped(*args, **kwargs):
                try:
                    with _write_scope(operation):
                        return function(*args, **kwargs)
                except PinConflict as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error

        wrapped.__signature__ = inspect.signature(function, eval_str=True)
        return wrapped

    return decorate


class MutationBusy(PinConflict):
    pass


def claim_training_version_write(db: Session, event: ChannelQueueEvent):
    if not isinstance(event, ChannelQueueEvent) or object_session(event) is not db:
        return None
    guard = db.get(ChannelLlmVersionGuard, event.bot_version_id, populate_existing=True)
    if guard is None:
        return None
    _lock_version(db, event.bot_version_id)
    event = db.scalar(
        select(ChannelQueueEvent)
        .where(ChannelQueueEvent.id == event.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    guard = db.get(ChannelLlmVersionGuard, event.bot_version_id, populate_existing=True)
    if event.status not in ("queued", "processing"):
        raise MutationBusy("이미 처리된 학습 작업입니다.")
    if guard.mutation_status == "idle":
        begin_version_mutation(db, event.bot_version_id, "training")
        guard = db.get(ChannelLlmVersionGuard, event.bot_version_id)
    elif guard.mutation_status != "running" or guard.owner != "queue:" + str(event.id):
        raise MutationBusy("다른 writer 또는 종료 불명 작업이 이 버전을 사용 중입니다.")
    guard.fence += 1
    guard.mutation_token = uuid4()
    guard.owner = "worker:" + str(event.id) + ":" + uuid4().hex
    guard.heartbeat_at = datetime.now(timezone.utc)
    token = MutationToken(event.bot_version_id, guard.mutation_token, guard.fence)
    db.commit()
    return token


@contextmanager
def training_version_write(db: Session, token: MutationToken | None):
    if token is None:
        yield
        return
    context = _WriteContext("training", db=db, tokens={token.version_id: token})
    handle = _active_write.set(context)
    try:
        try:
            yield
        except Exception:
            _finish_context(context, True)
            raise
        else:
            _finish_context(context, False)
    finally:
        _active_write.reset(handle)


def quarantine_interrupted_training(db: Session, event: ChannelQueueEvent):
    if not isinstance(event, ChannelQueueEvent) or object_session(event) is not db:
        return False
    guard = db.get(ChannelLlmVersionGuard, event.bot_version_id)
    if guard is None:
        return False
    _lock_version(db, event.bot_version_id)
    guard = db.get(ChannelLlmVersionGuard, event.bot_version_id, populate_existing=True)
    guard.mutation_status = "unknown"
    guard.heartbeat_at = datetime.now(timezone.utc)
    return True


def retains_persistent_history(db: Session, version: BotVersion):
    if not isinstance(version, BotVersion) or object_session(version) is not db:
        return False
    return (
        db.scalar(
            select(ChannelLlmBinding.id)
            .where(ChannelLlmBinding.version_id == version.id)
            .limit(1)
        )
        is not None
    )


def remove_unused_version_guard(db: Session, version: BotVersion):
    if not isinstance(version, BotVersion) or object_session(version) is not db:
        return
    guard = db.get(ChannelLlmVersionGuard, version.id)
    if guard is not None:
        _lock_version(db, version.id, include_deleted=True)
        context = _active_write.get()
        own_token = context.tokens.get(version.id) if context is not None else None
        if own_token is not None:
            assert_version_mutation_token(db, own_token)
            context.removed_guards.add(version.id)
        elif guard.mutation_status != "idle":
            raise PinConflict("쓰기 종료 확인 전 버전을 정리할 수 없습니다.")
        db.delete(guard)
        db.flush()
