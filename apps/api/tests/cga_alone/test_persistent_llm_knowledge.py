"""복제본 실제 ML 재학습의 원본 불변과 미검증 공유 검색 지식의 실패 닫힘."""

from copy import deepcopy
from hashlib import sha256
import json
from uuid import UUID
import pytest
from sqlalchemy.orm import Session
from app.models import Bot, BotVersion, User, ChannelQueueEvent
from app.api.routes import bots
from app.core.config import settings
from app.schemas.bot import VersionUpdateRequest
from app.services.version_pins import PinConflict, open_pinned_dialog
from tests.integration.test_bot_persistence_regression import _request


def test_unproven_shared_rag_cannot_claim_pinned_knowledge(persistent_context):
    c = persistent_context
    with Session(c.engine) as db:
        db.get(Bot, c.bot_id).data_json = {
            "answer_mode": "llm_rag",
            "vector_connections": {"answer": {"index_name": "mutable"}},
        }
        db.commit()
        with pytest.raises(PinConflict):
            open_pinned_dialog(
                db,
                c.scope,
                {"knowledge": {"immutable": True, "version_id": str(c.version_id)}},
            )


def test_copy_retrain_preserves_source_document_model_and_instructions(
    persistent_context, monkeypatch, tmp_path
):
    c = persistent_context
    monkeypatch.setenv("AIDOT_ML_ACCELERATOR", "cpu")
    monkeypatch.setattr(settings, "nlu_model_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "nlu_training_worker_enabled", True)

    def train(db, user, version_id):
        result = bots.enqueue_version_nlu_training(
            c.bot_id, version_id, _request(), None, user, db
        )["data"]
        job = db.get(ChannelQueueEvent, UUID(result["job_id"]))
        completed = bots._process_nlu_training_queue_event(db, job)
        assert completed["status"] == "completed", completed

    with Session(c.engine) as db:
        bot = db.get(Bot, c.bot_id)
        bot.data_json = {
            "nlu_type": "ml",
            "nlu_engine": "deep_learning_lite",
            "nlu_model": "deep_learning_lite",
            "answer_mode": "fixed",
            "language": "ko",
        }
        source = db.get(BotVersion, c.version_id)
        doc = deepcopy(source.version_json)
        doc["dialogs"] = [
            {
                "id": f"source-{i}",
                "name": f"원본 의도{i}",
                "dialogType": "1",
                "utterances": [{"text": f"원본 지식 질문{i}", "utteranceType": "T"}],
            }
            for i in range(2)
        ]
        doc["system_config"]["instructions"] = "원본 고정 지침"
        bots._assign_version_document(source, doc)
        db.commit()
        user = db.get(User, c.user_id)
        train(db, user, c.version_id)
        db.refresh(source)
        before = sha256(
            json.dumps(source.version_json, sort_keys=True).encode()
        ).hexdigest()
        source_path = (
            tmp_path / str(c.bot_id) / str(c.version_id) / "deep_learning_lite.json"
        )
        model_before = sha256(source_path.read_bytes()).hexdigest()
        binding = open_pinned_dialog(db, c.scope, {"instructions": "원본 고정 지침"})
        db.commit()
        copied = bots.copy_version(c.bot_id, c.version_id, _request(), user, db)["data"]
        target_id = UUID(copied["id"])
        doc = deepcopy(copied["version_json"])
        doc["system_config"]["instructions"] = "복제본 새 지침"
        doc["dialogs"] = [
            {
                "id": f"new-{i}",
                "name": f"새 의도{i}",
                "dialogType": "1",
                "utterances": [{"text": f"복제본 다른 지식{i}", "utteranceType": "T"}],
            }
            for i in range(2)
        ]
        bots.update_version(
            c.bot_id,
            target_id,
            VersionUpdateRequest(version_json=doc),
            _request(),
            user,
            db,
        )
        train(db, user, target_id)
        db.refresh(source)
        assert (
            sha256(json.dumps(source.version_json, sort_keys=True).encode()).hexdigest()
            == before
        )
        assert sha256(source_path.read_bytes()).hexdigest() == model_before
        assert (
            tmp_path / str(c.bot_id) / str(target_id) / "deep_learning_lite.json"
        ).exists()
        assert binding.snapshot_json["instructions"] == "원본 고정 지침"
