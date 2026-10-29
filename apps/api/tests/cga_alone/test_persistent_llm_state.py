"""기존 방을 사용하는 영속 매핑과 격리 제약을 검증한다."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import ChannelLlmBinding, ChannelRoom
from app.services.version_pins import (
    PinConflict,
    open_pinned_dialog,
    close_pinned_dialog,
)


def test_binding_survives_session_restart_and_freezes_snapshot(persistent_context):
    c = persistent_context
    snapshot = {"instructions": "첫 지침", "knowledge_revision": "r1"}
    with Session(c.engine) as db:
        binding = open_pinned_dialog(db, c.scope, snapshot)
        snapshot["instructions"] = "바뀐 지침"
        binding.provider_config_ref = "qa-project"
        binding.provider_conversation_id = "conv-test-" + uuid4().hex
        db.commit()
        identifier = binding.id
        provider_id = binding.provider_conversation_id
    with Session(c.engine) as db:
        binding = db.get(ChannelLlmBinding, identifier)
        assert binding.snapshot_json["instructions"] == "첫 지침"
        assert (
            binding.provider_conversation_id == provider_id
            and binding.version_id == c.version_id
        )


def test_room_bot_and_principal_scope_cannot_be_rebound(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, replace(c.scope, bot_id=uuid4()), {})
        db.rollback()
        with pytest.raises(PinConflict):
            open_pinned_dialog(
                db, replace(c.scope, principal_key="user:" + str(uuid4())), {}
            )


def test_new_generation_requires_explicit_close(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        first = open_pinned_dialog(db, c.scope, {"revision": "r1"})
        db.commit()
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, replace(c.scope, generation=2), {})
        db.rollback()
        close_pinned_dialog(db, first.id)
        db.commit()
        second = open_pinned_dialog(
            db, replace(c.scope, generation=2), {"revision": "r2"}
        )
        db.commit()
        assert second.id != first.id and second.provider_conversation_id is None
        assert second.snapshot_json == {"revision": "r2"}


def test_closed_history_prevents_legacy_hard_purge(persistent_context):
    from app.api.routes import bots
    from app.models import BotVersion

    c = persistent_context
    with Session(c.engine) as db:
        binding = open_pinned_dialog(db, c.scope, {})
        db.commit()
        close_pinned_dialog(db, binding.id)
        db.commit()
        version = db.get(BotVersion, c.version_id)
        bots._hard_delete_bot_version(db, version)
        db.commit()
        assert db.get(BotVersion, c.version_id) is not None
        assert db.get(ChannelLlmBinding, binding.id).status == "closed"


def test_deleted_closed_version_number_is_not_reused(persistent_context):
    from app.api.routes import bots
    from app.models import User, BotVersion

    c = persistent_context
    with Session(c.engine) as db:
        user = db.get(User, c.user_id)
        copied = bots.copy_version(
            c.bot_id,
            c.version_id,
            __import__(
                "tests.integration.test_bot_persistence_regression",
                fromlist=["_request"],
            )._request(),
            user,
            db,
        )["data"]
        target = __import__("uuid").UUID(copied["id"])
        room = ChannelRoom(
            channel_type="simulator",
            client_room_id=str(uuid4()),
            bot_id=c.bot_id,
            bot_version_id=target,
            participant_id=str(c.user_id),
        )
        db.add(room)
        db.flush()
        binding = open_pinned_dialog(
            db, replace(c.scope, room_id=room.id, version_id=target), {}
        )
        db.commit()
        close_pinned_dialog(db, binding.id)
        db.commit()
        from tests.integration.test_bot_persistence_regression import _request

        bots.delete_version(c.bot_id, target, _request(), user, db)
        next_copy = bots.copy_version(c.bot_id, c.version_id, _request(), user, db)[
            "data"
        ]
        assert next_copy["version_no"] == 3
        assert db.get(BotVersion, target).deleted_at is not None
        assert db.get(ChannelLlmBinding, binding.id).status == "closed"


def test_unused_sample_copy_can_be_deleted(persistent_context):
    from app.api.routes import bots
    from app.models import User, BotVersion
    from tests.integration.test_bot_persistence_regression import _request
    from uuid import UUID

    c = persistent_context
    with Session(c.engine) as db:
        user = db.get(User, c.user_id)
        copied = bots.copy_version(c.bot_id, c.version_id, _request(), user, db)["data"]
        identifier = UUID(copied["id"])
        bots.delete_version(c.bot_id, identifier, _request(), user, db)
        assert db.get(BotVersion, identifier) is None
