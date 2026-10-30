"""S04 서버 발급 Webchat 대화 자격 증명. 실제 Provider 호출 없음."""
from contextlib import contextmanager
from copy import deepcopy
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func
from tests.cga_alone.test_two_stage_runtime import native_secondary_document, fake_answer_http
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.integration.test_bot_persistence_regression import _request
from app.api.routes import bots,channels,webchat
from app.core.config import settings
from app.models import AdminChannel,ChannelRoom,ChannelMessage,ChannelQueueEvent
from app.schemas.bot import VersionUpdateRequest

@pytest.fixture
def credential_context(secondary_bot,native_secondary_document,monkeypatch):
    bot,version,_,user,db=secondary_bot
    graph=native_secondary_document["dialog_flow_graphs"][0]
    graph["nodes"].insert(1,{"id":"followup","kind":"talk","config":{"messageType":"text","basicMessages":["추가 설명"],"responseType":"relay","responseVariableName":"followup"}})
    graph["links"][0]["targetNodeId"]="followup"
    graph["links"].append({"sourceNodeId":"followup","sourcePort":"next","targetNodeId":"function"})
    bots.update_version(bot.id,version.id,VersionUpdateRequest(version_json=native_secondary_document),_request(),current_user=user,db=db)
    bots.activate_version(bot.id,version.id,_request(),current_user=user,db=db)
    db.add(AdminChannel(organization_id=bot.organization_id,code="WEBCHAT",name="QA",status="active"));db.flush()
    @contextmanager
    def session():yield db
    monkeypatch.setattr(channels,"SessionLocal",session);monkeypatch.setattr(webchat,"SessionLocal",session)
    monkeypatch.setattr(settings,"webchat_api_key","s04-qa-key")
    monkeypatch.setattr(channels,"_select_dialog_for_bot",lambda *a,**kw:(None,0.0))
    monkeypatch.setattr(webchat,"_select_dialog_for_bot",lambda *a,**kw:(None,0.0))
    calls=fake_answer_http(monkeypatch)
    def send(text="질문",token=None,room="client-room",actor="A"):
        return webchat.create_webchat_room_message(str(bot.id),room,webchat.WebchatMessageRequest(message=text,participant_id=actor),_request(),"s04-qa-key",token)["data"]
    return bot,version,db,calls,send

def test_credential_allows_followup_and_stores_only_hash(credential_context):
    bot,version,db,calls,send=credential_context
    first=send();token=first["conversationToken"];room_id,secret=token.split(".",1)
    assert len(secret)>=43 and first["botMessage"]["text"]=="추가 설명" and not calls
    room=db.get(ChannelRoom,UUID(room_id));assert secret not in str(room.metadata_json)
    actor=room.participant_id;assert actor!="A"
    second=send("후속 설명",token=token,actor="다른 표시 이름")
    assert second["botMessage"]["text"]=="답변: 외부 생성 답변" and len(calls)==1
    assert room.participant_id==actor
    assert db.scalar(select(func.count()).select_from(ChannelRoom).where(ChannelRoom.bot_id==bot.id))==1

def test_without_credential_same_client_room_cannot_continue_someone_else(credential_context):
    bot,version,db,calls,send=credential_context
    first=send();room=db.get(ChannelRoom,UUID(first["conversationToken"].split(".",1)[0]));before=deepcopy(room.metadata_json)
    second=send("B 질문",actor="B")
    assert second["botMessage"]["text"]=="추가 설명" and calls==[]
    assert second["conversationToken"]!=first["conversationToken"] and room.metadata_json==before
    assert db.scalar(select(func.count()).select_from(ChannelRoom).where(ChannelRoom.bot_id==bot.id))==2

@pytest.mark.parametrize("kind",["wrong-secret","wrong-room","wrong-client","closed","deleted","expired","wrong-bot","wrong-org","missing-marker"])
def test_invalid_credential_cannot_change_state_or_call_api(credential_context,kind):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];rid,secret=token.split(".",1);room=db.get(ChannelRoom,UUID(rid));client="client-room"
    if kind=="wrong-secret":token=rid+"."+"x"*43
    if kind=="wrong-room":token=str(uuid4())+"."+secret
    if kind=="wrong-client":client="another-client-room"
    if kind=="closed":room.status="closed"
    if kind=="deleted":
        from datetime import datetime,timezone
        room.deleted_at=datetime.now(timezone.utc)
    if kind in {"expired","wrong-bot","wrong-org","missing-marker"}:
        metadata=deepcopy(room.metadata_json);binding=metadata["__webchatAccess"]
        if kind=="expired":binding["expires_at"]="2000-01-01T00:00:00+00:00"
        if kind=="wrong-bot":binding["bot_id"]=str(uuid4())
        if kind=="wrong-org":binding["organization_id"]=str(uuid4())
        if kind=="missing-marker":metadata.pop("__webchatAccess")
        room.metadata_json=metadata
    db.flush();before=deepcopy(room.metadata_json);count=db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==room.id))
    with pytest.raises(HTTPException) as error:send("침입 입력",token=token,room=client,actor="A")
    assert error.value.status_code==404 and calls==[] and room.metadata_json==before
    assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==room.id))==count

@pytest.mark.parametrize("action",["read","write","close","reuse","list","worker"])
def test_shared_channel_key_cannot_bypass_credential_room(credential_context,action):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];room=db.get(ChannelRoom,UUID(token.split(".",1)[0]));before=deepcopy(room.metadata_json)
    if action=="list":
        result=channels.list_channel_rooms("webchat",_request(),None,"s04-qa-key")["data"]
        assert str(room.id) not in str(result)
    else:
        with pytest.raises(HTTPException) as error:
            if action=="read":channels.get_channel_room("webchat",room.id,_request(),"s04-qa-key")
            if action=="write":channels.create_channel_room_message("webchat",room.id,channels.ChannelMessageRequest(message="주입"),_request(),"s04-qa-key")
            if action=="close":channels.delete_channel_room("webchat",room.id,_request(),"s04-qa-key")
            if action=="reuse":channels.create_channel_room("webchat",channels.ChannelRoomCreateRequest(bot_id=str(bot.id),client_room_id=room.client_room_id),_request(),"s04-qa-key")
            if action=="worker":
                event=ChannelQueueEvent(room_id=room.id,bot_id=bot.id,bot_version_id=version.id,channel_type="webchat",participant_id=room.participant_id,parameter_json={"message":"주입"},result_json={})
                channels._process_channel_queue_event(db,_request(),"webchat",event)
        assert error.value.status_code==404
    assert calls==[] and room.status=="open" and room.metadata_json==before


def test_http_header_transport_keeps_shared_key_and_rejects_tampering(credential_context,caplog):
    from fastapi.testclient import TestClient
    from app.main import app
    bot,version,db,calls,send=credential_context
    path=f"/api/v1/webchat/bots/{bot.id}/rooms/http-client/messages"
    with TestClient(app) as client:
        assert client.post(path,json={"message":"질문"}).status_code==401
        first=client.post(path,headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"message":"질문"})
        assert first.status_code==200
        token=first.json()["data"]["conversationToken"]
        wrong=client.post(path,headers={"X-Aidot-Webchat-Key":"s04-qa-key","X-Aidot-Conversation-Token":token+"x"},json={"message":"후속"})
        assert wrong.status_code==404 and not calls
        second=client.post(path,headers={"X-Aidot-Webchat-Key":"s04-qa-key","X-Aidot-Conversation-Token":token},json={"message":"후속"})
        assert second.status_code==200 and second.json()["data"]["botMessage"]["text"]=="답변: 외부 생성 답변" and len(calls)==1
    secret=token.split(".",1)[1]
    assert secret not in caplog.text
    room=db.get(ChannelRoom,UUID(token.split(".",1)[0]))
    assert secret not in str(room.metadata_json)
    events=list(db.scalars(select(ChannelQueueEvent).where(ChannelQueueEvent.room_id==room.id)))
    assert all(secret not in str(e.parameter_json)+str(e.result_json) for e in events)


def test_shared_queue_processor_skips_credential_rooms(credential_context):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];rid=UUID(token.split(".",1)[0])
    event=db.scalar(select(ChannelQueueEvent).where(ChannelQueueEvent.room_id==rid));event.status="queued";db.flush()
    result=channels._process_queued_channel_events(db,_request(),channel="webchat")
    assert result==[] and event.status=="queued" and not calls


def test_active_version_application_keeps_room_protected(credential_context):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];rid=UUID(token.split(".",1)[0]);room=db.get(ChannelRoom,rid)
    binding=deepcopy(room.metadata_json["__webchatAccess"])
    channels._apply_active_version_to_room(room,bot,version,"webchat");db.flush()
    assert room.metadata_json["__webchatAccess"]==binding
    assert webchat._validate_webchat_credential(db,bot,"client-room",token)[0]==rid
    with pytest.raises(HTTPException):channels.get_channel_room("webchat",rid,_request(),"s04-qa-key")
    assert not calls


def test_server_capability_is_reset_even_if_native_runtime_raises(credential_context,monkeypatch):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];rid=UUID(token.split(".",1)[0])
    def fail(*a,**kw):raise HTTPException(status_code=409,detail="synthetic execution rejection")
    monkeypatch.setattr(channels,"create_channel_room_message",fail)
    with pytest.raises(HTTPException):send("후속",token=token)
    with pytest.raises(HTTPException) as error:channels._assert_legacy_room_access(db,rid)
    assert error.value.status_code==404 and not calls


def test_persistent_protection_cannot_be_bypassed_by_webchat_credential(credential_context,monkeypatch):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"]
    monkeypatch.setattr(channels,"_is_persistent_room",lambda *args:True)
    with pytest.raises(HTTPException) as error:send("후속",token=token)
    assert error.value.status_code==404 and not calls


@pytest.mark.parametrize("lookup",["room","session","client","end"])
def test_am_adapter_cannot_read_or_end_credential_room(credential_context,monkeypatch,lookup):
    from app.api.routes import am
    bot,version,db,calls,send=credential_context
    @contextmanager
    def session():yield db
    monkeypatch.setattr(am,"SessionLocal",session)
    token=send()["conversationToken"];room=db.get(ChannelRoom,UUID(token.split(".",1)[0]));before=deepcopy(room.metadata_json)
    with pytest.raises(HTTPException) as error:
        if lookup=="room":am._resolve_room("webchat",room.id,None,None)
        if lookup=="session":am._resolve_room("webchat",None,str(room.id),None)
        if lookup=="client":am._resolve_room("webchat",None,None,room.client_room_id)
        if lookup=="end":am.end_am_session(str(bot.id),am.AmSessionEndRequest(roomId=str(room.id),channel_type="webchat"),_request(),"s04-qa-key")
    assert error.value.status_code==404 and not calls and room.status=="open" and room.metadata_json==before


def test_corrupted_stored_digest_is_denied_without_runtime(credential_context):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"];room=db.get(ChannelRoom,UUID(token.split(".",1)[0]))
    metadata=deepcopy(room.metadata_json);metadata["__webchatAccess"]["digest"]="잘못된 비ASCII hash";room.metadata_json=metadata;db.flush()
    with pytest.raises(HTTPException) as error:send("주입",token=token)
    assert error.value.status_code==404 and not calls


def test_credential_cannot_be_ignored_by_legacy_hub_path(credential_context,monkeypatch):
    bot,version,db,calls,send=credential_context
    token=send()["conversationToken"]
    bot.data_json={**bot.data_json,"bot_kind":"hub"};db.flush()
    classified=[]
    def classify(*a,**kw):classified.append(True);return None,0.0
    monkeypatch.setattr(webchat,"_select_dialog_for_bot",classify)
    with pytest.raises(HTTPException) as error:send("다른 경로 입력",token=token)
    assert error.value.status_code==404 and not classified and not calls
