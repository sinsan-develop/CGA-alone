"""S03: 제작자 지정 모듈 저장·운영 준비. 실제 모델 호출 없음."""
from uuid import UUID
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.api.routes import bots, channels
from app.models import Bot, BotVersion
from app.schemas.bot import BotCreateRequest, BotUpdateRequest
from tests.integration.test_bot_persistence_regression import _request

@pytest.fixture
def secondary_bot(regression_actor, db_session, monkeypatch):
    result = bots.create_bot(BotCreateRequest(name="S03 검증봇"), _request(), regression_actor, db_session)
    bot = db_session.get(Bot, UUID(result["data"]["id"]))
    version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == bot.id))
    document = {"dialogs": [{"id":"module-1", "name":"외부 답변", "displayName":"외부 답변", "dialogType":0}],
        "dialog_flow_graphs":[{"id":"graph-1", "dialogId":"module-1", "name":"외부 답변", "nodes":[{"id":"start", "kind":"start", "config":{}},{"id":"end", "kind":"end", "config":{}}], "links":[{"sourceNodeId":"start", "sourcePort":"next", "targetNodeId":"end"}]}],
        "system_config":{"ai_config":{"nlu_type":"ml", "nlu_model":"deep_learning_lite", "answer_mode":"fixed"}}}
    bots._assign_version_document(version, document)
    db_session.flush()
    monkeypatch.setattr(bots, "_require_version_operating_user", lambda *args: None)
    monkeypatch.setattr(bots, "_is_version_trained", lambda *args: True)
    return bot, version, document, regression_actor, db_session

def test_draft_can_save_without_secondary_module(secondary_bot):
    bot, version, _, user, db = secondary_bot
    result = bots.update_bot(bot.id, BotUpdateRequest(settings_scope=str(version.id), description="초안"), _request(), current_user=user, db=db)
    assert result["data"]["description"] == "초안"
    assert "secondary_llm" not in version.version_json["system_config"]["ai_config"]

def test_secondary_module_round_trip_and_rename_uses_stable_id(secondary_bot):
    bot, version, document, user, db = secondary_bot
    bots.update_bot(bot.id, BotUpdateRequest(settings_scope=str(version.id), secondary_llm={"module_id":"module-1"}), _request(), current_user=user, db=db)
    db.refresh(version)
    assert version.version_json["system_config"]["ai_config"]["secondary_llm"] == {"module_id":"module-1"}
    document = version.version_json
    document["dialogs"][0]["name"] = "표시명 변경"
    bots._assign_version_document(version, document)
    assert bots._get_version_ai_config(bot, version)["secondary_llm"]["module_id"] == "module-1"

def test_activation_requires_valid_secondary_module(secondary_bot):
    bot, version, _, user, db = secondary_bot
    with pytest.raises(HTTPException) as error:
        bots.activate_version(bot.id, version.id, _request(), current_user=user, db=db)
    assert error.value.status_code == 400 and "2차" in str(error.value.detail)

@pytest.mark.parametrize("kind", ["deleted", "foreign", "intent"])
def test_deleted_or_foreign_module_cannot_activate(secondary_bot, kind):
    bot, version, document, user, db = secondary_bot
    document["system_config"]["ai_config"]["secondary_llm"] = {"module_id":"other-module" if kind == "foreign" else "module-1"}
    if kind == "deleted":document["dialogs"] = []
    if kind == "intent":document["dialogs"][0]["dialogType"] = 1
    bots._assign_version_document(version, document)
    db.flush()
    with pytest.raises(HTTPException) as error:
        bots.activate_version(bot.id, version.id, _request(), current_user=user, db=db)
    assert error.value.status_code == 400 and "2차" in str(error.value.detail)

def test_local_engine_remains_ml_or_semantic(secondary_bot):
    bot, version, document, user, db = secondary_bot
    document["system_config"]["ai_config"].update(nlu_type="llm", nlu_model="llm_engine_default", secondary_llm={"module_id":"module-1"})
    bots._assign_version_document(version, document)
    db.flush()
    with pytest.raises(HTTPException) as error:
        bots.activate_version(bot.id, version.id, _request(), current_user=user, db=db)
    assert error.value.status_code == 400 and "1차" in str(error.value.detail)

def test_valid_creator_module_can_activate(secondary_bot):
    bot, version, document, user, db = secondary_bot
    document["system_config"]["ai_config"]["secondary_llm"] = {"module_id":"module-1"}
    bots._assign_version_document(version, document)
    db.flush()
    result = bots.activate_version(bot.id, version.id, _request(), current_user=user, db=db)
    assert result["data"]["status"] == "active"

def test_runtime_preparation_rejects_missing_secondary_module(secondary_bot):
    bot, version, _, _, _ = secondary_bot
    assert "2차" in str(channels._runtime_block_reason(bot, version))


def test_copy_version_keeps_secondary_reference_inside_copied_document(secondary_bot):
    from app.services.bot_ai_policy import secondary_llm_block_reason
    bot, version, document, user, db = secondary_bot
    document["system_config"]["ai_config"]["secondary_llm"] = {"module_id":"module-1"}
    bots._assign_version_document(version, document)
    db.flush()
    copied = bots.copy_version(bot.id, version.id, _request(), user, db)
    copied_version = db.get(BotVersion, UUID(copied["data"]["id"]))
    assert copied_version.id != version.id
    assert secondary_llm_block_reason(copied_version.version_json) is None
    assert copied_version.version_json["system_config"]["ai_config"]["secondary_llm"] == {"module_id":"module-1"}


def test_existing_hub_routing_does_not_require_answer_module():
    from app.services.bot_ai_policy import secondary_llm_block_reason
    assert secondary_llm_block_reason({}, bot_kind="hub") is None
