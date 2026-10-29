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
from app.schemas.bot import VersionUpdateRequest, LlmIntentTestRequest
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
        prediction_before = bots.test_version_ml_intent(
            c.bot_id,
            c.version_id,
            LlmIntentTestRequest(utterance="원본 지식 질문0"),
            _request(),
            user,
            db,
        )["data"]["candidates"]
        assert prediction_before and prediction_before[0]["intent_id"] == "source-0"
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

        bots._purge_version_cache(
            source
        )  # 합성 버전 UUID 범위만, global Redis flush 금지.
        db.expire_all()
        prediction_after = bots.test_version_ml_intent(
            c.bot_id,
            c.version_id,
            LlmIntentTestRequest(utterance="원본 지식 질문0"),
            _request(),
            db.get(User, c.user_id),
            db,
        )["data"]["candidates"]
        assert prediction_after == prediction_before
        # 새 OS process/새 DB session으로 모델을 다시 읽어 process cache 재사용도 배제한다.
        import os, subprocess, sys
        from pathlib import Path

        env = os.environ.copy()
        env["NLU_MODEL_STORAGE_PATH"] = str(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                COLD_KNOWLEDGE_WORKER,
                str(c.bot_id),
                str(c.version_id),
                str(c.user_id),
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        assert result.returncode == 0, result.stderr[-1500:]
        assert json.loads(result.stdout.splitlines()[-1]) == prediction_before


@pytest.mark.parametrize(
    "nlu_type", ["semantic", "semantic_external", "semantic_vector"]
)
@pytest.mark.parametrize(
    "hint", [{}, {"knowledge": {"immutable": True, "kind": "local"}}]
)
def test_unproven_semantic_nlu_cannot_bypass_server_profile(
    nlu_type, hint, persistent_context
):
    c = persistent_context
    with Session(c.engine) as db:
        db.get(Bot, c.bot_id).data_json = {
            "nlu_type": nlu_type,
            "answer_mode": "fixed",
            "vector_connections": {"intent": {"index_name": "unproven-shared"}},
        }
        db.commit()
        with pytest.raises(PinConflict):
            open_pinned_dialog(db, c.scope, hint)


COLD_KNOWLEDGE_WORKER = r"""
import os,sys,json
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.api.routes import bots
from app.models import User
from app.schemas.bot import LlmIntentTestRequest
from tests.integration.test_bot_persistence_regression import _request
url=os.environ['AIDOT_TEST_DATABASE_URL'];assert url.rsplit('/',1)[-1]=='cga_alone_qa'
engine=create_engine(url,pool_pre_ping=True)
from app.core.config import settings
assert (settings.nlu_model_storage_dir/sys.argv[1]/sys.argv[2]/'deep_learning_lite.json').is_file(), 'QA 모델 경로 설정이 필요합니다'
with Session(engine) as db:
    result=bots.test_version_ml_intent(UUID(sys.argv[1]),UUID(sys.argv[2]),LlmIntentTestRequest(utterance='원본 지식 질문0'),_request(),db.get(User,UUID(sys.argv[3])),db)
    print(json.dumps(result['data']['candidates'],ensure_ascii=False))
engine.dispose()
"""
