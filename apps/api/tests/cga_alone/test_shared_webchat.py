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



def send_shared(c,member,text="질문",addressed=True,display_actor="forged"):
    return webchat.create_webchat_room_message(str(c[0].id),member["roomId"],webchat.WebchatMessageRequest(message=text,participant_id=display_actor,addressed=addressed),_request(),"s04-qa-key",member["conversationToken"])["data"]


def test_shared_turn_retains_owner_until_scenario_end(credential_context):
    c=credential_context;a=create(c);b=join(c,a["roomId"])
    first=send_shared(c,a)
    assert first["botMessage"]["text"]=="추가 설명" and c[3]==[]
    assert history(c,a["roomId"],a["conversationToken"])["turn"]["canAskBot"] is True
    assert history(c,a["roomId"],b["conversationToken"])["turn"]=={"state":"waiting_input","ownerParticipantId":a["participantId"],"canAskBot":False}
    room=c[2].get(ChannelRoom,UUID(a["roomId"]));before=deepcopy(room.metadata_json)
    count=c[2].scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==room.id))
    with pytest.raises(HTTPException) as error:send_shared(c,b,"대신 답변")
    assert error.value.status_code==409 and room.metadata_json==before and not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==room.id))==count
    second=send_shared(c,a,"후속 설명")
    assert len(c[3])==1 and second["botMessage"]["text"]=="답변: 외부 생성 답변"
    assert history(c,a["roomId"],b["conversationToken"])["turn"]["state"]=="idle"
    assert send_shared(c,b)["botMessage"]["text"]=="추가 설명"


def test_shared_general_chat_is_saved_without_claim_or_bot_execution(credential_context):
    c=credential_context;a=create(c);b=join(c,a["roomId"])
    send_shared(c,a)
    room=c[2].get(ChannelRoom,UUID(a["roomId"]));variables=deepcopy(room.metadata_json.get("variables"));turn=deepcopy(room.metadata_json["__roomTurn"])
    response=send_shared(c,b,"사람끼리 대화",addressed=False)
    assert response["botMessage"] is None and response["botMessages"]==[] and not c[3]
    assert room.metadata_json["__roomTurn"]==turn and room.metadata_json.get("variables")==variables
    messages=history(c,a["roomId"],a["conversationToken"])["messages"]
    assert messages[-1]["text"]=="사람끼리 대화" and messages[-1]["participantId"]==b["participantId"] and messages[-1]["participantName"]=="B"


def test_shared_room_does_not_treat_missing_addressed_as_bot_call(credential_context):
    c=credential_context;a=create(c)
    response=send_shared(c,a,"봇 이름이 언급된 일반 대화",addressed=None)
    assert response["botMessages"]==[] and not c[3]
    assert history(c,a["roomId"],a["conversationToken"])["turn"]["state"]=="idle"


def test_shared_member_body_cannot_impersonate_other_member_and_rooms_are_independent(credential_context):
    c=credential_context;a=create(c);b=join(c,a["roomId"]);other=create(c)
    send_shared(c,a)
    with pytest.raises(HTTPException) as error:send_shared(c,b,"도용",display_actor=a["participantId"])
    assert error.value.status_code==409
    assert send_shared(c,other)["botMessage"]["text"]=="추가 설명"
    private=c[4]();assert private["botMessage"]["text"]=="추가 설명" and not c[3]
    assert send_shared(c,a,"후속",display_actor=b["participantId"])["userMessage"]["participantId"]==a["participantId"]



def test_shared_output_preserves_general_chat_and_join_during_external_answer(credential_context,monkeypatch):
    import json
    c=credential_context;a=create(c);send_shared(c,a);joined=[]
    class Response:
        status=200;reason="OK"
        def __enter__(self):return self
        def __exit__(self,*args):return None
        def read(self):return json.dumps({"phase":"ok","answer":"완료"}).encode()
    def answer(request,timeout):
        b=join(c,a["roomId"]);joined.append(b)
        send_shared(c,b,"외부 응답 중 일반 대화",addressed=False)
        return Response()
    monkeypatch.setattr(channels,"urlopen",answer)
    result=send_shared(c,a,"후속 입력")
    assert result["botMessage"]["text"]=="답변: 완료"
    stored=history(c,a["roomId"],joined[0]["conversationToken"])
    assert len(stored["messages"])==5
    room=c[2].get(ChannelRoom,UUID(a["roomId"]));history_state=room.metadata_json["conversationHistory"]
    assert history_state["message_count"]==5
    assert "외부 응답 중 일반 대화" in history_state["user_raw_utterances"]
    assert len(history_state["transcript"])==5


from tests.cga_alone.test_room_turn import turn_context


@pytest.fixture
def committed_shared_context(turn_context,native_secondary_document,monkeypatch):
    from contextlib import contextmanager
    from threading import Thread,Event
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.parse import urlparse,parse_qs
    from sqlalchemy.orm import Session
    from sqlalchemy import delete
    from app.models import Bot,BotVersion,AdminChannel,AuditLog,ChannelQueueEvent
    from app.api.routes import bots
    from app.core.config import settings
    import json
    c=turn_context;c.entered=Event();c.release=Event();c.http_calls=[];c.native_sessions=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            question=parse_qs(urlparse(self.path).query).get("q",[""])[0];c.http_calls.append(question)
            if question=="A 질문" and c.http_calls.count(question)==1:
                c.entered.set();c.release.wait(14)
            raw=json.dumps({"phase":"ok","answer":"로컬 HTTP 대역 답변"}).encode()
            self.send_response(500 if question=="오류 질문" else 200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    worker=Thread(target=server.serve_forever,name="s05-http-stub",daemon=True);worker.start()
    document=deepcopy(native_secondary_document);document["apis"][0]["baseUrl"]="http://127.0.0.1:"+str(server.server_port)
    graph=document["dialog_flow_graphs"][0]
    graph["nodes"].insert(1,{"id":"followup","kind":"talk","config":{"messageType":"text","basicMessages":["추가 설명"],"responseType":"relay","responseVariableName":"followup"}})
    graph["links"][0]["targetNodeId"]="followup";graph["links"].append({"sourceNodeId":"followup","sourcePort":"next","targetNodeId":"function"})
    with Session(c.engine) as db:
        bot=db.get(Bot,c.bot_id);version=db.get(BotVersion,c.version_id)
        bots._assign_version_document(version,document);version.status="active";bot.active_version_id=version.id
        db.add(AdminChannel(organization_id=c.org_id,code="WEBCHAT",name="S05 소유 QA",status="active"));db.commit()
    @contextmanager
    def factory():
        with Session(c.engine) as db:
            context=channels._shared_turn_capability.get()
            if context:c.native_sessions.append((context["actor_id"],db))
            yield db
    monkeypatch.setattr(channels,"SessionLocal",factory);monkeypatch.setattr(webchat,"SessionLocal",factory)
    monkeypatch.setattr(settings,"webchat_api_key","s05-qa-key")
    monkeypatch.setattr(channels,"_select_dialog_for_bot",lambda *a,**kw:(None,0.0))
    c.client=TestClient(app);c.base="/api/v1/webchat/bots/"+str(c.bot_id);c.headers={"X-Aidot-Webchat-Key":"s05-qa-key"}
    try:yield c
    finally:
        c.release.set();c.client.close();server.shutdown();server.server_close();worker.join(timeout=3)
        assert not worker.is_alive()
        with Session(c.engine) as db:
            rooms=db.scalars(select(ChannelRoom.id).where(ChannelRoom.bot_id==c.bot_id)).all()
            events=db.scalars(select(ChannelQueueEvent.id).where(ChannelQueueEvent.bot_id==c.bot_id)).all()
            db.execute(delete(AuditLog).where(AuditLog.target_id.in_(rooms+events)))
            db.execute(delete(AdminChannel).where(AdminChannel.organization_id==c.org_id));db.commit()


def committed_create(c):
    response=c.client.post(c.base+"/shared-rooms",headers=c.headers,json={})
    assert response.status_code==200
    return response.json()["data"]


def committed_send(c,member,text,addressed=True):
    return c.client.post(c.base+"/rooms/"+member["roomId"]+"/messages",headers={**c.headers,"X-Aidot-Conversation-Token":member["conversationToken"]},json={"message":text,"addressed":addressed,"participant_id":"untrusted-body"})


def test_shared_http_wait_has_no_db_transaction_and_other_rooms_progress(committed_shared_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy.orm import Session
    from app.models import ChannelQueueEvent
    c=committed_shared_context;a=committed_create(c)
    b=c.client.post(c.base+"/shared-rooms/"+a["roomId"]+"/participants",headers=c.headers,json={"participant_name":"B"}).json()["data"]
    other=committed_create(c)
    assert committed_send(c,a,"A 질문").status_code==200
    pool=ThreadPoolExecutor(max_workers=1)
    future=pool.submit(committed_send,c,a,"추가 설명")
    try:
        assert c.entered.wait(5)
        with Session(c.engine) as db:
            room=db.get(ChannelRoom,UUID(a["roomId"]));before=deepcopy(room.metadata_json.get("variables"));first_expiry=room.metadata_json["__roomTurn"]["processing_expires_at"]
            assert db.scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==room.id,ChannelQueueEvent.status=="processing"))==1
        assert all(not session.in_transaction() for actor,session in c.native_sessions if actor==a["participantId"])
        # A의 외부 실행 중에도 다른 연결에서 이 방의 짧은 lock을 취할 수 있다.
        with Session(c.engine) as db:assert db.scalar(select(ChannelRoom).where(ChannelRoom.id==UUID(a["roomId"])).with_for_update(nowait=True)) is not None
        assert committed_send(c,b,"대신 답변").status_code==409
        assert committed_send(c,a,"동일 참가자의 중복 처리").status_code==409
        with Session(c.engine) as db:assert db.get(ChannelRoom,UUID(a["roomId"])).metadata_json.get("variables")==before
        assert committed_send(c,b,"일반 대화",False).status_code==200
        late=c.client.post(c.base+"/shared-rooms/"+a["roomId"]+"/participants",headers=c.headers,json={"participant_name":"후발"}).json()["data"]
        assert committed_send(c,other,"C 질문").status_code==200
        assert committed_send(c,other,"C 후속").json()["data"]["botMessage"]["text"]=="답변: 로컬 HTTP 대역 답변"
        private=c.client.post(c.base+"/rooms/private/messages",headers=c.headers,json={"message":"D 질문"}).json()["data"]
        private_second=c.client.post(c.base+"/rooms/private/messages",headers={**c.headers,"X-Aidot-Conversation-Token":private["conversationToken"]},json={"message":"D 후속"})
        assert private_second.json()["data"]["botMessage"]["text"]=="답변: 로컬 HTTP 대역 답변"
        # 실제 10초 heartbeat가 별도 transaction으로 lease를 늘리는지 확인한다.
        Event().wait(10.3)
        with Session(c.engine) as db:
            room=db.get(ChannelRoom,UUID(a["roomId"]))
            assert room.metadata_json["__roomTurn"]["processing_expires_at"]>first_expiry
        assert all(not session.in_transaction() for actor,session in c.native_sessions if actor==a["participantId"])
        c.release.set();response=future.result(timeout=5)
        assert response.status_code==200 and response.json()["data"]["turn"]["state"]=="idle"
        response=c.client.get(c.base+"/shared-rooms/"+a["roomId"]+"/messages",headers={**c.headers,"X-Aidot-Conversation-Token":late["conversationToken"]})
        assert response.status_code==200 and len(response.json()["data"]["messages"])==5
        with Session(c.engine) as db:
            history_state=db.get(ChannelRoom,UUID(a["roomId"])).metadata_json["conversationHistory"]
            assert history_state["message_count"]==5 and "일반 대화" in history_state["user_raw_utterances"]
        assert sorted(c.http_calls)==["A 질문","C 질문","D 질문"]
    finally:
        c.release.set();pool.shutdown(wait=True,cancel_futures=True)



def test_expired_shared_execution_cannot_continue_old_scenario_or_write_late_answer(committed_shared_context):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import Session
    from app.models import ChannelQueueEvent
    c=committed_shared_context;a=committed_create(c)
    b=c.client.post(c.base+"/shared-rooms/"+a["roomId"]+"/participants",headers=c.headers,json={}).json()["data"]
    assert committed_send(c,a,"A 질문").status_code==200
    pool=ThreadPoolExecutor(max_workers=1);future=pool.submit(committed_send,c,a,"A 후속")
    try:
        assert c.entered.wait(5)
        with Session(c.engine) as db:
            room=db.get(ChannelRoom,UUID(a["roomId"]));metadata=deepcopy(room.metadata_json)
            metadata["__roomTurn"]["processing_expires_at"]="2000-01-01T00:00:00+00:00";room.metadata_json=metadata;db.commit()
        response=committed_send(c,b,"B 새 질문")
        with Session(c.engine) as db:
            assert db.scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(a["roomId"]),ChannelQueueEvent.status=="unknown"))==1
        assert response.status_code==200 and response.json()["data"]["botMessage"]["text"]=="추가 설명"
        with Session(c.engine) as db:before=deepcopy(db.get(ChannelRoom,UUID(a["roomId"])).metadata_json)
        c.release.set();late=future.result(timeout=5)
        assert late.status_code==409
        with Session(c.engine) as db:
            room=db.get(ChannelRoom,UUID(a["roomId"]))
            assert room.metadata_json==before and room.metadata_json["__roomTurn"]["actor_id"]==b["participantId"]
            assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==room.id,ChannelMessage.text.like("%로컬 HTTP 대역 답변%")))==0
            assert db.scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==room.id,ChannelQueueEvent.status=="unknown"))==1
        assert c.http_calls==["A 질문"]
    finally:
        c.release.set();pool.shutdown(wait=True,cancel_futures=True)



def test_shared_provider_error_releases_turn_without_automatic_retry(committed_shared_context):
    c=committed_shared_context;a=committed_create(c)
    b=c.client.post(c.base+"/shared-rooms/"+a["roomId"]+"/participants",headers=c.headers,json={}).json()["data"]
    assert committed_send(c,a,"오류 질문").status_code==200
    response=committed_send(c,a,"추가 설명")
    assert response.status_code==200 and response.json()["data"]["turn"]["state"]=="idle"
    assert c.http_calls==["오류 질문"]
    assert committed_send(c,b,"새 질문").json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c.http_calls==["오류 질문"]


def test_shared_binding_and_waiting_scenario_survive_new_api_processes(committed_shared_context):
    import subprocess,sys,json,os
    c=committed_shared_context;a=committed_create(c)
    script="""
import json,os,sys
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.main import app
from app.api.routes import channels,webchat
from app.core.config import settings
packet=json.load(sys.stdin)
url=os.environ['AIDOT_TEST_DATABASE_URL']
assert make_url(url).database=='cga_alone_qa'
engine=create_engine(url)
@contextmanager
def factory():
    with Session(engine) as db:yield db
channels.SessionLocal=factory;webchat.SessionLocal=factory
channels._select_dialog_for_bot=lambda *a,**kw:(None,0.0)
settings.webchat_api_key='s05-qa-key'
client=TestClient(app)
response=client.post('/api/v1/webchat/bots/'+packet['bot']+'/rooms/'+packet['room']+'/messages',json={'message':packet['message'],'addressed':True},headers={'X-Aidot-Webchat-Key':'s05-qa-key','X-Aidot-Conversation-Token':packet['credential']})
data=response.json().get('data',{})
print('S05_RESULT='+json.dumps({'pid':os.getpid(),'status':response.status_code,'text':(data.get('botMessage') or {}).get('text'),'turn':data.get('turn'),'actor':(data.get('userMessage') or {}).get('participantId')},ensure_ascii=True),flush=True)
client.close();engine.dispose()
"""
    def run(message):
        result=subprocess.run([sys.executable,"-c",script],input=json.dumps({"bot":str(c.bot_id),"room":a["roomId"],"credential":a["conversationToken"],"message":message}),text=True,capture_output=True,timeout=25,env=os.environ.copy())
        assert result.returncode==0,"소유 QA API 하위 프로세스 검증 실패"
        lines=[line for line in result.stdout.splitlines() if line.startswith("S05_RESULT=")]
        assert len(lines)==1
        return json.loads(lines[0].split("=",1)[1])
    first=run("재시작 질문")
    assert first["status"]==200 and first["text"]=="추가 설명" and first["turn"]["state"]=="waiting_input"
    second=run("후속 설명")
    assert first["pid"]!=second["pid"] and second["status"]==200
    assert second["text"]=="답변: 로컬 HTTP 대역 답변" and second["turn"]["state"]=="idle"
    assert first["actor"]==second["actor"]==a["participantId"]
    assert c.http_calls==["재시작 질문"]



def test_expired_waiting_member_is_not_continued_by_new_questioner(credential_context):
    c=credential_context;a=create(c);b=join(c,a["roomId"]);send_shared(c,a)
    room=c[2].get(ChannelRoom,UUID(a["roomId"]));metadata=deepcopy(room.metadata_json)
    metadata["__roomTurn"]["waiting_expires_at"]="2000-01-01T00:00:00+00:00";room.metadata_json=metadata;c[2].commit()
    result=send_shared(c,b,"독립 새 질문")
    assert result["botMessage"]["text"]=="추가 설명" and not c[3]
    assert room.metadata_json["__roomTurn"]["actor_id"]==b["participantId"]


def test_shared_end_session_immediately_closes_room_and_keeps_access_marker(credential_context):
    from app.api.routes import bots
    c=credential_context;a=create(c)
    document=deepcopy(c[1].version_json)
    end=next(node for node in document["dialog_flow_graphs"][0]["nodes"] if node["id"]=="end")
    end["config"]={"endSessionImmediately":True};bots._assign_version_document(c[1],document);c[2].flush()
    send_shared(c,a);result=send_shared(c,a,"후속")
    room=c[2].get(ChannelRoom,UUID(a["roomId"]))
    assert result["runtime"]["sessionEnded"] is True and room.status=="closed"
    assert "__roomTurn" not in room.metadata_json and room.metadata_json["__webchatAccess"]["mode"]=="shared"
    with pytest.raises(HTTPException) as error:history(c,a["roomId"],a["conversationToken"])
    assert error.value.status_code==404 and len(c[3])==1
