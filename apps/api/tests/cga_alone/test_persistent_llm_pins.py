"""열린 대화의 전체 참조 수명과 실제 PG 경쟁을 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from app.models import ChannelLlmBinding, ChannelLlmVersionGuard, ChannelRoom
from app.services.version_pins import (
    PinConflict,
    open_pinned_dialog,
    close_pinned_dialog,
    begin_version_mutation,
    finish_version_mutation,
)


def test_idle_open_binding_keeps_version_pinned(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        binding = open_pinned_dialog(db, c.scope, {})
        binding.updated_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
        with pytest.raises(PinConflict):
            begin_version_mutation(db, c.version_id, "edit")


def test_end_does_not_unpin(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        open_pinned_dialog(db, c.scope, {})
        db.get(ChannelRoom, c.room_id).metadata_json = {"scenario_ended": True}
        db.commit()
        with pytest.raises(PinConflict):
            begin_version_mutation(db, c.version_id, "training")


def test_last_reference_controls_unpin(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        first = open_pinned_dialog(db, c.scope, {})
        room = ChannelRoom(
            channel_type="simulator",
            client_room_id=str(uuid4()),
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            participant_id=str(c.user_id),
        )
        db.add(room)
        db.flush()
        second = open_pinned_dialog(db, replace(c.scope, room_id=room.id), {})
        db.commit()
        close_pinned_dialog(db, first.id)
        db.commit()
        with pytest.raises(PinConflict):
            begin_version_mutation(db, c.version_id, "edit")
        db.rollback()
        close_pinned_dialog(db, second.id)
        db.commit()
        token = begin_version_mutation(db, c.version_id, "edit")
        assert token is not None
        finish_version_mutation(db, token, "a" * 64)
        db.commit()


def _race(c, operation):
    barrier = Barrier(2)

    def run(kind):
        with Session(c.engine) as db:
            barrier.wait(timeout=10)
            try:
                if kind == "open":
                    open_pinned_dialog(db, c.scope, {})
                else:
                    begin_version_mutation(db, c.version_id, operation)
                db.commit()
                return "allowed"
            except PinConflict:
                db.rollback()
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(run, ("open", "mutation")))


def test_open_races_edit_only_one_wins(persistent_context):
    assert _race(persistent_context, "edit") == ["allowed", "blocked"]


def test_open_races_training_lease(persistent_context):
    assert _race(persistent_context, "training") == ["allowed", "blocked"]


def test_orphan_writer_does_not_unlock_by_ttl(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        token = begin_version_mutation(db, c.version_id, "training")
        db.get(ChannelLlmVersionGuard, c.version_id).heartbeat_at = datetime.now(
            timezone.utc
        ) - timedelta(days=30)
        db.commit()
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, c.scope, {})
        db.rollback()
        with pytest.raises(PinConflict):
            finish_version_mutation(db, replace(token, fence=token.fence + 1), "a" * 64)


def test_real_version_update_rejects_open_pin(persistent_context):
    from fastapi import HTTPException
    from app.api.routes import bots
    from app.models import User, BotVersion
    from app.schemas.bot import VersionUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request

    c = persistent_context
    with Session(c.engine) as db:
        open_pinned_dialog(db, c.scope, {})
        db.commit()
        with pytest.raises(HTTPException) as denied:
            bots.update_version(
                c.bot_id,
                c.version_id,
                VersionUpdateRequest(description="바꾸면 안 됨"),
                _request(),
                db.get(User, c.user_id),
                db,
            )
        assert denied.value.status_code == 409
        db.rollback()
        assert db.get(BotVersion, c.version_id).description is None


def test_queued_training_keeps_mutation_lease_before_worker(
    persistent_context, monkeypatch
):
    from app.api.routes import bots
    from app.models import User
    from app.core.config import settings
    from tests.integration.test_bot_persistence_regression import _request

    c = persistent_context
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)
    with Session(c.engine) as db:
        job = bots.enqueue_version_nlu_training(
            c.bot_id, c.version_id, _request(), None, db.get(User, c.user_id), db
        )["data"]
        guard = db.get(ChannelLlmVersionGuard, c.version_id, populate_existing=True)
        assert (
            guard.mutation_status == "running"
            and guard.owner == "queue:" + job["job_id"]
        )
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, c.scope, {})


def test_registered_sample_edit_releases_lease_after_success(persistent_context):
    from app.api.routes import bots
    from app.models import User, BotVersion
    from app.schemas.bot import VersionUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request

    c = persistent_context
    with Session(c.engine) as db:
        result = bots.update_version(
            c.bot_id,
            c.version_id,
            VersionUpdateRequest(description="새 버전 편집"),
            _request(),
            db.get(User, c.user_id),
            db,
        )
        assert result["data"]["description"] == "새 버전 편집"
        guard = db.get(ChannelLlmVersionGuard, c.version_id, populate_existing=True)
        assert guard.mutation_status == "idle" and guard.fence > 0


def test_validation_failure_before_write_releases_sample_lease(
    persistent_context, monkeypatch
):
    from fastapi import HTTPException
    from app.api.routes import bots
    from app.models import User
    from app.schemas.bot import VersionUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request

    c = persistent_context

    def invalid(*args, **kwargs):
        raise HTTPException(status_code=400, detail="합성 검증 실패")

    monkeypatch.setattr(bots, "_serialize_version", invalid)
    with Session(c.engine) as db:
        with pytest.raises(HTTPException):
            bots.update_version(
                c.bot_id,
                c.version_id,
                VersionUpdateRequest(description="저장 안 됨"),
                _request(),
                db.get(User, c.user_id),
                db,
            )
        db.rollback()
        guard = db.get(ChannelLlmVersionGuard, c.version_id, populate_existing=True)
        assert guard.mutation_status == "idle" and guard.fence > 0


def test_actual_worker_finishes_queued_sample_lease(persistent_context, monkeypatch):
    from app.api.routes import bots
    from app.models import User, ChannelQueueEvent
    from app.core.config import settings
    from tests.integration.test_bot_persistence_regression import _request

    c = persistent_context
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)

    def trainer(bot_id, version_id, request, payload, current_user, db):
        guard = db.get(ChannelLlmVersionGuard, version_id, populate_existing=True)
        assert guard.owner.startswith("worker:")
        return {"data": {"model": {"counts": {"intents": 2}}}}

    monkeypatch.setattr(bots, "_run_version_nlu_training", trainer)
    with Session(c.engine) as db:
        result = bots.enqueue_version_nlu_training(
            c.bot_id, c.version_id, _request(), None, db.get(User, c.user_id), db
        )["data"]
        event = db.get(ChannelQueueEvent, __import__("uuid").UUID(result["job_id"]))
        completed = bots._process_nlu_training_queue_event(db, event)
        assert completed["status"] == "completed"
        guard = db.get(ChannelLlmVersionGuard, c.version_id, populate_existing=True)
        assert guard.mutation_status == "idle" and guard.owner is None


def test_restarted_sample_worker_is_unknown_without_requeue(
    persistent_context, monkeypatch
):
    from app.api.routes import bots
    from app.models import User, ChannelQueueEvent
    from app.core.config import settings
    from tests.integration.test_bot_persistence_regression import _request
    from uuid import UUID

    c = persistent_context
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)
    with Session(c.engine) as db:
        result = bots.enqueue_version_nlu_training(
            c.bot_id, c.version_id, _request(), None, db.get(User, c.user_id), db
        )["data"]
        event = db.get(ChannelQueueEvent, UUID(result["job_id"]))
        event.status = "processing"
        db.commit()
        bots.recover_interrupted_nlu_training_events(db)
        assert (
            db.get(ChannelQueueEvent, event.id, populate_existing=True).status
            != "queued"
        )
        assert (
            db.get(
                ChannelLlmVersionGuard, c.version_id, populate_existing=True
            ).mutation_status
            == "unknown"
        )
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, c.scope, {})


def test_copy_settings_can_change_while_source_dialog_is_open(persistent_context):
    from app.api.routes import bots
    from app.models import User, BotVersion
    from app.schemas.bot import BotUpdateRequest
    from tests.integration.test_bot_persistence_regression import _request
    from uuid import UUID

    c = persistent_context
    with Session(c.engine) as db:
        open_pinned_dialog(db, c.scope, {"instructions": "원본 지침"})
        db.commit()
        user = db.get(User, c.user_id)
        copied = bots.copy_version(c.bot_id, c.version_id, _request(), user, db)["data"]
        result = bots.update_bot(
            c.bot_id,
            BotUpdateRequest(
                settings_scope=copied["id"], settings_json={"cutOffScore": 0.55}
            ),
            _request(),
            response_mode="full",
            response_version_scope=None,
            current_user=user,
            db=db,
        )
        assert result["data"]["id"] == str(c.bot_id)
        assert db.get(BotVersion, c.version_id).version_json != None
        assert (
            db.get(
                ChannelLlmVersionGuard, UUID(copied["id"]), populate_existing=True
            ).mutation_status
            == "idle"
        )


def test_worker_timeout_retains_unknown_lease(persistent_context, monkeypatch):
    from app.api.routes import bots
    from app.models import User, ChannelQueueEvent
    from app.core.config import settings
    from app.services.version_pins import mark_external_version_write
    from tests.integration.test_bot_persistence_regression import _request
    from uuid import UUID

    c = persistent_context
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)

    def timeout(bot_id, version_id, request, payload, current_user, db):
        mark_external_version_write(version_id)
        raise TimeoutError("합성 외부 writer 결과 불명")

    monkeypatch.setattr(bots, "_run_version_nlu_training", timeout)
    with Session(c.engine) as db:
        job = bots.enqueue_version_nlu_training(
            c.bot_id, c.version_id, _request(), None, db.get(User, c.user_id), db
        )["data"]
        result = bots._process_nlu_training_queue_event(
            db, db.get(ChannelQueueEvent, UUID(job["job_id"]))
        )
        assert result["status"] == "failed"
        assert (
            db.get(
                ChannelLlmVersionGuard, c.version_id, populate_existing=True
            ).mutation_status
            == "unknown"
        )
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, c.scope, {})


def test_duplicate_workers_cannot_claim_same_sample_writer(
    persistent_context, monkeypatch
):
    from app.api.routes import bots
    from app.models import User, ChannelQueueEvent
    from app.core.config import settings
    from app.services.version_pins import claim_training_version_write, MutationBusy
    from tests.integration.test_bot_persistence_regression import _request
    from uuid import UUID

    c = persistent_context
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)
    with Session(c.engine) as db:
        job = bots.enqueue_version_nlu_training(
            c.bot_id, c.version_id, _request(), None, db.get(User, c.user_id), db
        )["data"]
    barrier = Barrier(2)

    def claim(_):
        with Session(c.engine) as db:
            event = db.get(ChannelQueueEvent, UUID(job["job_id"]))
            barrier.wait(timeout=10)
            try:
                claim_training_version_write(db, event)
                return "allowed"
            except MutationBusy:
                db.rollback()
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == ["allowed", "blocked"]


def test_last_close_races_new_open_without_allowing_mutation_and_live_pin(
    persistent_context,
):
    from sqlalchemy import select

    c = persistent_context
    with Session(c.engine) as db:
        first = open_pinned_dialog(db, c.scope, {})
        first_id = first.id
        room = ChannelRoom(
            channel_type="simulator",
            client_room_id=str(uuid4()),
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            participant_id=str(c.user_id),
        )
        db.add(room)
        db.flush()
        next_scope = replace(c.scope, room_id=room.id)
        db.commit()
    barrier = Barrier(2)

    def close_and_edit():
        with Session(c.engine) as db:
            barrier.wait(timeout=10)
            close_pinned_dialog(db, first_id)
            try:
                token = begin_version_mutation(db, c.version_id, "edit")
            except PinConflict:
                token = None
            db.commit()
            return token

    def open_next():
        with Session(c.engine) as db:
            barrier.wait(timeout=10)
            try:
                binding = open_pinned_dialog(db, next_scope, {})
                db.commit()
                return binding.id
            except PinConflict:
                db.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        closing = pool.submit(close_and_edit)
        opening = pool.submit(open_next)
        token = closing.result(timeout=10)
        binding_id = opening.result(timeout=10)
    assert (token is not None) != (binding_id is not None)
    with Session(c.engine) as db:
        assert db.get(ChannelLlmBinding, first_id).status == "closed"
        if binding_id:
            assert db.get(ChannelLlmBinding, binding_id).status == "open"
            assert (
                db.get(ChannelLlmVersionGuard, c.version_id).mutation_status == "idle"
            )
        else:
            assert (
                db.get(ChannelLlmVersionGuard, c.version_id).mutation_status
                == "running"
            )
            finish_version_mutation(db, token, "d" * 64)
            db.commit()
