"""S07 실제 PG template 등록→조회→Talk 저장→native runtime. 외부 platform 미호출."""
import json
from pathlib import Path
from copy import deepcopy
from uuid import UUID
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.api.routes import admin, bots, channels
from app.models import Role, UserRole, Organization, AdminTemplate, BotVersion
from app.schemas.admin import TemplateCreateRequest
from app.schemas.bot import VersionUpdateRequest
from tests.cga_alone.test_client_contract import client_context, create, send
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.integration.test_bot_persistence_regression import _request


@pytest.fixture
def template_context(client_context, regression_actor, monkeypatch):
    c, client = client_context
    db = c[2]
    role = db.scalar(select(Role).where(Role.code == "system_manager"))
    if role is None:
        role = Role(code="system_manager", name="QA 관리자")
        db.add(role); db.flush()
    db.add(UserRole(user_id=regression_actor.id, role_id=role.id)); db.flush()
    organization = db.get(Organization, regression_actor.organization_id)
    monkeypatch.setattr(admin, "_get_default_organization", lambda session: organization)
    return client_context, regression_actor


@pytest.mark.parametrize("name", ["webchat", "kakao", "ms-teams"])
def test_template_register_reload_talk_runtime_round_trip(template_context, name):
    context, user = template_context
    c, client = context
    db = c[2]
    fixture = json.loads((Path(__file__).parent / "fixtures/client_templates" / (name + ".json")).read_text(encoding="utf-8"))
    result = admin.create_admin_template(TemplateCreateRequest(channel_code=fixture["code"], name="S07 " + name,
        renderer_type=fixture["renderer"], item_types="text,card"), _request(), db=db, current_user=user)
    template_id = result["data"]["id"]
    db.expire_all()
    listed = admin.list_admin_templates(_request(), query="S07 " + name, channel_code=fixture["code"], status_filter=None,
        active_only=True, db=db, current_user=user)["data"]["items"]
    assert len(listed) == 1 and str(listed[0]["id"]) == template_id
    choices = bots.list_talk_templates(_request(), channel_code=fixture["code"], db=db, current_user=user)["data"]["items"]
    selected = next(item for item in choices if str(item["id"]) == template_id)
    assert selected["renderer_type"] == fixture["renderer"]
    version = db.get(BotVersion, c[1].id)
    document = deepcopy(version.version_json)
    talk = next(node for node in document["dialog_flow_graphs"][0]["nodes"] if node["id"] == "followup")
    talk["config"]["channelTemplates"] = {fixture["code"]: fixture["talk"]}
    bots.update_version(c[0].id, version.id, VersionUpdateRequest(version_json=document), _request(), current_user=user, db=db)
    db.expire_all()
    saved = db.get(BotVersion, version.id).version_json
    saved_talk = next(node for node in saved["dialog_flow_graphs"][0]["nodes"] if node["id"] == "followup")
    assert saved_talk["config"]["channelTemplates"][fixture["code"]] == fixture["talk"]
    room = create(context, name)
    response = send(context, room, "템플릿 질문", channel=name)
    assert response.status_code == 200, response.text
    message = response.json()["data"]["botMessage"]
    assert message["messageType"] == fixture["type"]
    assert message["text"] == "한국어 답변\n둘째 줄"
    assert message["payload"]["sourceTalkNodeId"] == "followup"
    if name == "webchat": assert message["payload"]["options"] == ["확인", "다음"]
    if name == "kakao":
        wire = channels._kakao_response_from_serialized_messages([message])
        assert wire["version"] == "2.0"
        assert wire["template"]["outputs"][0]["basicCard"]["title"] == "한글 카드"
    if name == "ms-teams":
        card = json.loads(fixture["talk"]["messages"][0])
        assert message["payload"]["adaptiveCard"] == card
        assert json.loads(message["payload"]["rawJson"]) == card
    assert not c[3]


@pytest.mark.parametrize("code,renderer", [("KAKAO", "form-a-card"), ("TEAMS", "basic-card"), ("WEBCHAT", "list-card")])
def test_unsupported_cross_platform_template_is_explicitly_rejected(template_context, code, renderer):
    context, user = template_context
    db = context[0][2]
    with pytest.raises(HTTPException) as error:
        admin.create_admin_template(TemplateCreateRequest(channel_code=code, name="지원하지 않는 표현", renderer_type=renderer), _request(), db=db, current_user=user)
    assert error.value.status_code == 400
    assert db.scalar(select(AdminTemplate).where(AdminTemplate.organization_id == user.organization_id, AdminTemplate.name == "지원하지 않는 표현")) is None
