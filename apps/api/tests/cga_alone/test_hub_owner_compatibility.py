"""기존 허브 기능과 제작자 학습자료 격리를 함께 검증한다."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_current_user, get_db
from app.api.routes import bots
from app.main import app
from app.models import Bot, BotHub, BotHubMember, BotVersion, User
from app.schemas.bot import BotCreateRequest
from tests.integration.test_bot_persistence_regression import _request

PRIVATE_TEXT = "다른 제작자가 작성한 비공개 학습문장 고유표식"


@pytest.fixture
def hub_context(regression_actor, db_session):
    response = bots.create_bot(BotCreateRequest(name="기존 자연어 허브", bot_kind="hub"), _request(), regression_actor, db_session)
    hub_bot = db_session.get(Bot, UUID(response["data"]["id"]))
    hub_version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == hub_bot.id))
    hub = db_session.get(BotHub, hub_bot.id)
    hub.call_method = "natural"
    children = []
    owners = []
    for number in range(2):
        owner = User(organization_id=regression_actor.organization_id, group_id=regression_actor.group_id,
                     login_id=f"hub-child-{uuid4().hex}", password_hash="test-only", name=f"하위 제작자 {number}", status="active")
        db_session.add(owner)
        db_session.flush()
        created = bots.create_bot(BotCreateRequest(name=f"연결 봇 {number}"), _request(), owner, db_session)
        child = db_session.get(Bot, UUID(created["data"]["id"]))
        version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == child.id))
        document = deepcopy(version.version_json)
        document["dialogs"] = [{"id":f"intent-{number}", "name":f"의도 {number}", "dialogType":"1",
                                "utterances":[{"text":PRIVATE_TEXT+str(number), "utteranceType":"T"}]}]
        document["system_config"]["nlu_training"] = {"status":"success", "intent_count":1, "utterance_count":1}
        bots._assign_version_document(version, document)
        version.status = "active"
        child.active_version_id = version.id
        db_session.add(BotHubMember(hub_id=hub_bot.id, bot_id=child.id, sort_order=number))
        children.append(child)
        owners.append(owner)
    db_session.flush()
    bots._prepare_natural_hub_training_document(db_session, hub_bot, hub_version)
    document = deepcopy(hub_version.version_json)
    document["system_config"]["nlu_training"] = {"status":"success", "intent_count":2, "utterance_count":2,
        "snapshot":{"intents":[{"utterances":[PRIVATE_TEXT]}]}}
    document["system_config"]["nlu_evaluation"] = {"snapshot":{"text":PRIVATE_TEXT}, "quality_diagnostics":{"example":PRIVATE_TEXT}}
    bots._assign_version_document(hub_version, document)
    hub_bot.active_version_id = hub_version.id
    db_session.commit()
    return hub_bot, hub_version, children, owners


@pytest.fixture
def hub_client(regression_actor, db_session):
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: regression_actor
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def test_hub_candidates_restore_connections_without_documents(hub_context, hub_client):
    hub, version, children, owners = hub_context
    response = hub_client.get(f"/api/v1/hubs/{hub.id}/candidates")
    assert response.status_code == 200, response.text
    candidates = response.json()["data"]
    assert {item["id"] for item in candidates} == {str(child.id) for child in children}
    assert all(set(item) == {"id", "name", "active_version_id", "is_trained"} for item in candidates)
    assert all(item["is_trained"] for item in candidates)
    assert PRIVATE_TEXT not in response.text
    assert {item["id"] for item in hub_client.get("/api/v1/bots").json()["data"]} == {str(hub.id)}


def test_other_owner_cannot_enumerate_hub_candidates(hub_context, hub_client):
    hub, version, children, owners = hub_context
    app.dependency_overrides[get_current_user] = lambda: owners[0]
    assert hub_client.get(f"/api/v1/hubs/{hub.id}/candidates").status_code == 404
    assert hub_client.get(f"/api/v1/bots/{hub.id}").status_code == 404


@pytest.mark.parametrize("suffix", ["", "/dialogs", "/configure", "/retraining", "/references", "/export", "/aidot-package",
                                    "/dialogs/{dialog_id}/flow", "/context", "/settings-context"])
def test_hub_read_and_exports_do_not_expose_child_training(suffix, hub_context, hub_client, db_session):
    hub, version, children, owners = hub_context
    dialog_id = f"hub-member-{children[0].id}"
    response = hub_client.get(f"/api/v1/bots/{hub.id}/versions/{version.id}" + suffix.format(dialog_id=dialog_id))
    assert response.status_code == 200, response.text
    assert PRIVATE_TEXT not in response.text
    db_session.refresh(version)
    assert PRIVATE_TEXT in str(version.version_json), "서버 학습/실행 원문은 보존해야 한다"
    assert len(bots._build_intent_vector_documents(version.version_json)) == 2


def test_hub_model_download_does_not_expose_training(hub_context, hub_client, monkeypatch):
    hub, version, children, owners = hub_context
    monkeypatch.setattr(bots, "get_deep_learning_lite_model_manifest", lambda *_: {"exists":True, "model":{"trained_at":"2026-10-29", "training_utterances":[PRIVATE_TEXT]}})
    response = hub_client.get(f"/api/v1/bots/{hub.id}/versions/{version.id}/nlu/model")
    assert response.status_code == 200
    assert response.json()["data"]["exists"] is True
    assert PRIVATE_TEXT not in response.text


def test_general_bot_owner_still_sees_own_training(hub_context, hub_client):
    hub, version, children, owners = hub_context
    child = children[0]
    app.dependency_overrides[get_current_user] = lambda: owners[0]
    response = hub_client.get(f"/api/v1/bots/{child.id}/versions/{child.active_version_id}/dialogs")
    assert response.status_code == 200
    assert PRIVATE_TEXT in response.text


def test_candidates_exclude_other_group_deleted_and_hubs(hub_context, hub_client, db_session):
    from datetime import datetime, timezone
    from app.models import Group
    hub, version, children, owners = hub_context
    group = Group(organization_id=hub.organization_id, code=f"other-{uuid4().hex}", name="다른 그룹")
    db_session.add(group)
    db_session.flush()
    db_session.add_all([
        Bot(organization_id=hub.organization_id, group_id=group.id, name="다른 그룹 봇", data_json={}),
        Bot(organization_id=hub.organization_id, group_id=hub.group_id, name="삭제 봇", deleted_at=datetime.now(timezone.utc), data_json={}),
        Bot(organization_id=hub.organization_id, group_id=hub.group_id, name="다른 허브", data_json={"bot_kind":"hub"}),
    ])
    db_session.commit()
    response = hub_client.get(f"/api/v1/hubs/{hub.id}/candidates")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["data"]} == {str(child.id) for child in children}


def test_hub_kind_change_does_not_expose_derived_documents(hub_context, hub_client):
    hub, version, children, owners = hub_context
    response = hub_client.patch(f"/api/v1/bots/{hub.id}", json={"bot_kind":"bot"})
    assert response.status_code == 200, response.text
    assert PRIVATE_TEXT not in response.text
    for suffix in ("", "/export", "/settings-context"):
        response = hub_client.get(f"/api/v1/bots/{hub.id}/versions/{version.id}" + suffix)
        assert response.status_code == 200
        assert PRIVATE_TEXT not in response.text


def test_hub_package_compatibility_source_is_not_exported(hub_context, hub_client, db_session):
    from app.services.aidot_package_compatibility import AIDOT_PACKAGE_META_KEY
    hub, version, children, owners = hub_context
    document = deepcopy(version.version_json)
    document[AIDOT_PACKAGE_META_KEY] = {"source_package":{"unmapped_private_training":PRIVATE_TEXT}, "mapped_hash":"old"}
    bots._assign_version_document(version, document)
    db_session.commit()
    for suffix in ("/export", "/aidot-package"):
        response = hub_client.get(f"/api/v1/bots/{hub.id}/versions/{version.id}" + suffix)
        assert response.status_code == 200
        assert PRIVATE_TEXT not in response.text


def test_legacy_hub_training_job_manifest_is_private(hub_context, hub_client, db_session):
    from app.models import ChannelQueueEvent
    hub, version, children, owners = hub_context
    event = ChannelQueueEvent(channel_type=bots.NLU_TRAINING_QUEUE_CHANNEL, bot_id=hub.id, bot_version_id=version.id,
                              status="completed", error_message=PRIVATE_TEXT, result_json={"manifest":{"model":{"training_utterances":[PRIVATE_TEXT], "intent_count":2}}})
    db_session.add(event)
    db_session.commit()
    response = hub_client.get(f"/api/v1/bots/{hub.id}/versions/{version.id}/nlu/train/jobs/{event.id}")
    assert response.status_code == 200
    assert PRIVATE_TEXT not in response.text


def test_hub_classification_does_not_return_training_features(hub_context, hub_client, monkeypatch):
    hub, version, children, owners = hub_context
    monkeypatch.setattr(bots, "score_deep_learning_lite_model", lambda *args, **kwargs: [{"dialog_id":"route", "dialog_name":"연결 봇", "score":0.99, "features":[PRIVATE_TEXT]}])
    response = hub_client.post(f"/api/v1/bots/{hub.id}/versions/{version.id}/nlu/ml/test", json={"utterance":"사용자 질문", "top_k":1})
    assert response.status_code == 200
    assert response.json()["data"]["candidates"][0]["intent_id"] == "route"
    assert PRIVATE_TEXT not in response.text


def test_natural_hub_still_trains_and_routes_locally(hub_context, db_session, monkeypatch, tmp_path):
    from app.core.config import settings
    hub, version, children, owners = hub_context
    monkeypatch.setattr(settings, "nlu_model_storage_path", str(tmp_path))
    monkeypatch.setenv("AIDOT_ML_ACCELERATOR", "cpu")
    result = bots._run_version_nlu_training(hub.id, version.id, _request(), None,
                                            db_session.get(User, hub.created_by), db_session)
    assert PRIVATE_TEXT not in str(result)
    assert result["data"]["model"]["counts"]["intents"] == 2
    manifest = bots.get_deep_learning_lite_model_manifest(hub.id, version.id)
    assert manifest["exists"] is True
    db_session.refresh(version)
    assert PRIVATE_TEXT in str(version.version_json), "서버 학습 입력 원문은 보존한다"
    assert manifest["model"]["counts"]["intents"] == 2
    scores = bots.score_deep_learning_lite_model(version, PRIVATE_TEXT+"0")
    assert scores
    assert scores[0]["dialog_id"] in {f"hub-member-{child.id}" for child in children}


def test_hub_candidates_require_login(hub_context, hub_client):
    hub, version, children, owners = hub_context
    app.dependency_overrides.pop(get_current_user)
    assert hub_client.get(f"/api/v1/hubs/{hub.id}/candidates").status_code == 401


def test_hub_privacy_preserves_rich_form_and_card_fields(hub_context):
    hub, version, children, owners = hub_context
    form = {"nodes":[{"id":"form", "data":{"reason":"선택 이유", "model":{"name":"폼 데이터"},
            "properties":{"utterances":{"type":"string", "title":"입력값"}}}}]}
    payload = {"dialogs":deepcopy(version.version_json["dialogs"]), "dialog_flow_graphs":[form]}
    original = deepcopy(payload)
    visible = bots._public_hub_payload(hub, payload)
    assert visible["dialog_flow_graphs"] == original["dialog_flow_graphs"]
    assert payload == original, "조회 투영이 내부 원본을 수정하면 안 된다"
    assert PRIVATE_TEXT not in str(visible["dialogs"])
