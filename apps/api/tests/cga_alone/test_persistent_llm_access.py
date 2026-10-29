"""공유 키 legacy 경로가 보호 방을 우회하지 못하는 실제PG 회귀."""

from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker
from app.models import Bot, BotVersion, ChannelRoom, ChannelQueueEvent, User
from app.api.routes import channels, bots
from app.core.config import settings
from app.services.version_pins import open_pinned_dialog
from tests.integration.test_bot_persistence_regression import _request


@pytest.fixture
def protected(persistent_context, monkeypatch):
    c = persistent_context
    monkeypatch.setattr(channels, "SessionLocal", sessionmaker(bind=c.engine))
    monkeypatch.setattr(settings, "webchat_api_key", "synthetic-shared-key")
    with Session(c.engine) as db:
        bot = db.get(Bot, c.bot_id)
        version = db.get(BotVersion, c.version_id)
        bot.active_version_id = version.id
        version.status = "active"
        room = db.get(ChannelRoom, c.room_id)
        room.channel_type = "webchat"
        client_room_id = room.client_room_id
        open_pinned_dialog(db, c.scope, {"instructions": "고정 지침"})
        db.commit()
    return c, client_room_id


@pytest.mark.parametrize("action", ["read", "write", "close", "reuse"])
def test_shared_key_cannot_read_write_close_or_rebind_protected_room(protected, action):
    c, client_id = protected
    request = _request()
    key = "synthetic-shared-key"
    with pytest.raises(HTTPException) as error:
        if action == "read":
            channels.get_channel_room("webchat", c.room_id, request, key)
        elif action == "write":
            channels.create_channel_room_message(
                "webchat",
                c.room_id,
                channels.ChannelMessageRequest(message="질문"),
                request,
                key,
            )
        elif action == "close":
            channels.delete_channel_room("webchat", c.room_id, request, key)
        else:
            channels.create_channel_room(
                "webchat",
                channels.ChannelRoomCreateRequest(
                    bot_id=str(c.bot_id),
                    client_room_id=client_id,
                    start_immediately=False,
                ),
                request,
                key,
            )
    assert error.value.status_code in {403, 404}
    with Session(c.engine) as db:
        room = db.get(ChannelRoom, c.room_id)
        assert (
            room.status == "open"
            and room.bot_version_id == c.version_id
            and room.client_room_id == client_id
        )


def test_client_participant_is_not_identity(protected):
    c, _ = protected
    result = channels.list_channel_rooms(
        "webchat", _request(), str(c.user_id), "synthetic-shared-key"
    )["data"]
    assert all(item["id"] != str(c.room_id) for item in result["rooms"])


def test_disabling_feature_does_not_remove_room_acl(protected):
    c, _ = protected
    assert getattr(settings, "persistent_llm_enabled", False) is False
    with pytest.raises(HTTPException) as error:
        channels.get_channel_room(
            "webchat", c.room_id, _request(), "synthetic-shared-key"
        )
    assert error.value.status_code in {403, 404}


def test_legacy_worker_never_executes_persistent_event(protected, monkeypatch):
    c, _ = protected
    executed = []
    monkeypatch.setattr(
        channels,
        "_process_channel_queue_event",
        lambda db, request, channel, event: executed.append(event.id)
        or {"id": str(event.id)},
    )
    with Session(c.engine) as db:
        event = ChannelQueueEvent(
            room_id=c.room_id,
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            channel_type="webchat",
            operation_kind="persistent_llm",
            status="queued",
        )
        db.add(event)
        db.commit()
        assert (
            channels._process_queued_channel_events(db, _request()) == []
            and executed == []
        )
        assert db.get(ChannelQueueEvent, event.id).status == "queued"


def test_active_version_change_preserves_pinned_sample_only(protected):
    c, _ = protected
    with Session(c.engine) as db:
        ordinary = ChannelRoom(
            channel_type="webchat",
            client_room_id=str(uuid4()),
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            participant_id="ordinary",
        )
        db.add(ordinary)
        db.commit()
        count = bots._close_open_channel_rooms_for_version_change(
            db,
            _request(),
            db.get(User, c.user_id),
            db.get(Bot, c.bot_id),
            c.version_id,
            uuid4(),
        )
        db.commit()
        assert count == 1
        assert (
            db.get(ChannelRoom, c.room_id).status == "open"
            and db.get(ChannelRoom, ordinary.id).status == "closed"
        )
