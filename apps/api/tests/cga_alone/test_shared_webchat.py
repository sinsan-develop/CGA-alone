"""S05 승인 공유방 참가자 증명. 실제 PG, 실 Provider 호출 없음."""
from copy import deepcopy
from uuid import UUID,uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app.api.routes import channels,webchat
from app.main import app
from app.models import ChannelRoom,ChannelMessage
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.integration.test_bot_persistence_regression import _request


def create(c):
    return webchat.create_shared_webchat_room(str(c[0].id),webchat.WebchatSharedParticipantRequest(participant_name="A"),_request(),"s04-qa-key")["data"]


def join(c,room):
    return webchat.join_shared_webchat_room(str(c[0].id),UUID(room),webchat.WebchatSharedParticipantRequest(participant_name="B"),_request(),"s04-qa-key")["data"]


def history(c,room,token):
    return webchat.get_shared_webchat_messages(str(c[0].id),UUID(room),_request(),"s04-qa-key",token)["data"]


def test_create_join_issue_distinct_server_members_and_hash_only(credential_context):
    c=credential_context;first=create(c);second=join(c,first["roomId"])
    assert first["roomId"]==second["roomId"] and first["participantId"]!=second["participantId"]
    assert first["conversationToken"]!=second["conversationToken"] and not c[3]
    room=c[2].get(ChannelRoom,UUID(first["roomId"]))
    binding=room.metadata_json["__webchatAccess"]
    assert binding["mode"]=="shared" and len(binding["members"])==2
    assert room.client_room_id.startswith("wc-auth:")
    for result in [first,second]:
        assert result["conversationToken"].split(".",1)[1] not in str(room.metadata_json)
        assert result["participantId"] in binding["members"]
    assert history(c,first["roomId"],first["conversationToken"])["messages"]==[]


def test_private_room_cannot_be_joined_or_read_as_shared(credential_context):
    c=credential_context;private=c[4]();rid=private["conversationToken"].split(".",1)[0]
    room=c[2].get(ChannelRoom,UUID(rid));before=deepcopy(room.metadata_json)
    for operation in [lambda:join(c,rid),lambda:history(c,rid,private["conversationToken"])]:
        with pytest.raises(HTTPException) as error:operation()
        assert error.value.status_code==404
    assert room.metadata_json==before and not c[3]


def test_shared_history_is_member_only(credential_context):
    c=credential_context;first=create(c);second=join(c,first["roomId"])
    room=c[2].get(ChannelRoom,UUID(first["roomId"]))
    channels._add_message(c[2],room,participant_id=first["participantId"],participant_kind="user",participant_name="A",text="공유 질문")
    c[2].flush()
    a=history(c,first["roomId"],first["conversationToken"])
    b=history(c,first["roomId"],second["conversationToken"])
    assert a["messages"]==b["messages"] and a["messages"][0]["text"]=="공유 질문"
    assert a["turn"]=={"state":"idle","ownerParticipantId":None,"canAskBot":True}


@pytest.mark.parametrize("kind",["wrong-secret","wrong-room","expired","deleted","closed","wrong-org"])
def test_invalid_member_cannot_read_shared_history(credential_context,kind):
    c=credential_context;first=create(c);rid=first["roomId"];token=first["conversationToken"]
    room=c[2].get(ChannelRoom,UUID(rid));metadata=deepcopy(room.metadata_json)
    if kind=="wrong-secret":token=rid+"."+"x"*43
    if kind=="wrong-room":rid=create(c)["roomId"]
    if kind=="expired":metadata["__webchatAccess"]["members"][first["participantId"]]["expires_at"]="2000-01-01T00:00:00+00:00"
    if kind=="wrong-org":metadata["__webchatAccess"]["organization_id"]=str(uuid4())
    if kind=="closed":room.status="closed"
    if kind=="deleted":
        from datetime import datetime,timezone
        room.deleted_at=datetime.now(timezone.utc)
    room.metadata_json=metadata;c[2].flush();before=deepcopy(room.metadata_json)
    with pytest.raises(HTTPException) as error:history(c,rid,token)
    assert error.value.status_code==404 and room.metadata_json==before and not c[3]


def test_shared_routes_keep_existing_channel_key_and_no_creator_login(credential_context):
    c=credential_context;client=TestClient(app);base="/api/v1/webchat/bots/"+str(c[0].id)+"/shared-rooms"
    assert client.post(base,json={}).status_code==401
    headers={"X-Aidot-Webchat-Key":"s04-qa-key"}
    response=client.post(base,json={"participant_name":"A"},headers=headers)
    assert response.status_code==200
    first=response.json()["data"]
    response=client.post(base+"/"+first["roomId"]+"/participants",json={"participant_name":"B"},headers=headers)
    assert response.status_code==200
    assert client.get(base+"/"+first["roomId"]+"/messages",headers=headers).status_code==404
    headers["X-Aidot-Conversation-Token"]=first["conversationToken"]
    assert client.get(base+"/"+first["roomId"]+"/messages",headers=headers).status_code==200
    assert not c[3]
