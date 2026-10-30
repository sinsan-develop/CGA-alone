"""S11 native 표본→기존 질문 API/PG/Talk→Google Chat packet. 실 플랫폼/Provider 없음."""
import json
from pathlib import Path
from copy import deepcopy
from uuid import UUID
import subprocess
import pytest
from sqlalchemy import select, func
from app.models import AdminChannel, ChannelMessage, ChannelQueueEvent, ChannelRoom, BotVersion
from app.api.routes import bots, admin
from app.schemas.admin import TemplateCreateRequest
from app.schemas.bot import VersionUpdateRequest
from tests.cga_alone.test_client_contract import client_context, create, send, member_headers
from tests.cga_alone.test_client_templates import template_context
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.integration.test_bot_persistence_regression import _request

ROOT = Path(__file__).resolve().parents[5]
FIXTURE = json.loads((Path(__file__).parents[1]/"fixtures/client_templates/google-chat.json").read_text(encoding="utf-8"))


def sdk(operation, value):
    code = r"""
const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(p.root+'/apps/web/node_modules/typescript');
const source=fs.readFileSync(p.root+'/packages/shared/src/channel.ts','utf8'),holder={exports:{}};
new Function('exports','module','process',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(holder.exports,holder,process);
const fn=holder.exports[{normalize:'normalizeAidotGoogleChatEvent',render:'buildAidotGoogleChatReplyPayload'}[p.operation]];
if(!fn)console.log(JSON.stringify({missing:p.operation}));else (async()=>{try{const v=p.value;console.log(JSON.stringify({result:await (p.operation==='normalize'?fn(v.event,v.connection):fn(v.message,v.destination))}));}catch(e){console.log(JSON.stringify({error:e.message}));}})();
"""
    result=subprocess.run(["node","-e",code],input=json.dumps({"root":str(ROOT),"operation":operation,"value":value},ensure_ascii=False),text=True,capture_output=True,encoding="utf-8")
    assert result.returncode == 0, result.stderr
    data=json.loads(result.stdout);assert "missing" not in data, data
    if "error" in data:raise ValueError(data["error"])
    return data["result"]


def event(text=None,user=None,message_id=None,channel=None):
    item=deepcopy(FIXTURE["addressed"])
    if text is not None:
        item["message"].update(text=text,argumentText=text.removeprefix("@CgaAloneBot").strip(),annotations=item["message"]["annotations"] if text.startswith("@CgaAloneBot") else [])
    if user is not None:
        item["user"]["name"]=f"users/{user}"
        item["message"]["sender"]["name"]=f"users/{user}"
    if message_id is not None:item["message"]["name"]=f"spaces/QA_SPACE/messages/qa-message-{message_id}"
    if channel is not None:
        item["space"]["name"]=channel;item["message"]["space"]["name"]=channel
        item["message"]["name"]=channel+"/messages/qa-message-101";item["message"]["thread"]["name"]=channel+"/threads/QA_THREAD"
    return item


def normalized(native, connection=None):return sdk("normalize",{"event":native,"connection":connection or FIXTURE["connection"]})


@pytest.fixture
def google_chat_context(client_context):
    client_context[0][2].add(AdminChannel(organization_id=client_context[0][0].organization_id,code="GOOGLE_CHAT",name="S11 GoogleChat QA",status="active"))
    client_context[0][2].flush()
    return client_context


def post(context, member, native, connection=None):
    parsed=normalized(native,connection)
    return send(context,member,parsed["message"],"google-chat",addressed=parsed["addressed"],event=parsed["eventId"])


def test_addressed_message_invokes_bot(google_chat_context):
    c,_=google_chat_context;member=create(google_chat_context,"google-chat")
    result=post(google_chat_context,member,event());assert result.status_code==200
    assert result.json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==1
    assert post(google_chat_context,member,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    assert len(c[3])==1


def test_general_message_does_not_invoke_bot(google_chat_context):
    c,_=google_chat_context;member=create(google_chat_context,"google-chat")
    result=post(google_chat_context,member,FIXTURE["general"]);assert result.status_code==200 and result.json()["data"]["botMessages"]==[]
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==0


def test_other_actor_cannot_continue(google_chat_context):
    c,client=google_chat_context;first=create(google_chat_context,"google-chat")
    assert post(google_chat_context,first,event()).status_code==200
    joined=client.post(f"/api/v1/channels/google-chat/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":"B"});assert joined.status_code==200
    second={**joined.json()["data"],"room":first["room"]};room=c[2].get(ChannelRoom,UUID(first["roomId"]));before=deepcopy(room.metadata_json.get("variables"))
    assert post(google_chat_context,second,event(user=10000011,message_id=104)).status_code==409
    c[2].expire_all();assert room.metadata_json.get("variables")==before and not c[3]
    assert post(google_chat_context,second,FIXTURE["general"]).status_code==200


def test_duplicate_event_runs_once(google_chat_context):
    c,_=google_chat_context;member=create(google_chat_context,"google-chat")
    first=post(google_chat_context,member,event());assert first.status_code==200
    repeated=post(google_chat_context,member,event());assert repeated.status_code==200
    assert repeated.json()["data"]["userMessage"]["id"]==first.json()["data"]["userMessage"]["id"]
    answer=event("@CgaAloneBot 추가 설명",message_id=103)
    completed=post(google_chat_context,member,answer);assert completed.status_code==200
    assert post(google_chat_context,member,answer).json()["data"]["botMessage"]["id"]==completed.json()["data"]["botMessage"]["id"]
    assert len(c[3])==1
    assert post(google_chat_context,member,event("바뀐 내용",message_id=103)).status_code==409


def test_same_actor_in_different_rooms_is_isolated(google_chat_context):
    c,client=google_chat_context;first=create(google_chat_context,"google-chat");second=create(google_chat_context,"google-chat")
    other=event(channel="spaces/QA_OTHER");connection={**FIXTURE["connection"],"spaceName":"spaces/QA_OTHER"}
    assert normalized(event())["nativeRoomKey"]!=normalized(other,connection)["nativeRoomKey"]
    assert post(google_chat_context,first,event()).status_code==200 and post(google_chat_context,second,other,connection).status_code==200
    assert post(google_chat_context,first,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    result=client.get(f"/api/v1/channels/google-chat/rooms/{second['roomId']}",headers=member_headers(second));assert result.status_code==200
    assert result.json()["data"]["runtime"]["waitingNodeId"]=="followup" and len(c[3])==1


def test_template_round_trip_to_client_payload(google_chat_context,template_context):
    c,_=google_chat_context;_,user=template_context;db=c[2]
    template=admin.create_admin_template(TemplateCreateRequest(channel_code="GOOGLE_CHAT",name="S11 Google Chat 버튼",renderer_type="button",item_types="button"),_request(),db=db,current_user=user)["data"]
    assert any(str(item["id"])==template["id"] for item in bots.list_talk_templates(_request(),channel_code="GOOGLE_CHAT",db=db,current_user=user)["data"]["items"])
    document=deepcopy(c[1].version_json);node=next(item for item in document["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    node["config"]["channelTemplates"]={"GOOGLE_CHAT":FIXTURE["talk"]}
    bots.update_version(c[0].id,c[1].id,VersionUpdateRequest(version_json=document),_request(),current_user=user,db=db)
    db.expire_all();saved=db.get(BotVersion,c[1].id)
    assert next(item for item in saved.version_json["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")["config"]["channelTemplates"]["GOOGLE_CHAT"]==FIXTURE["talk"]
    member=create(google_chat_context,"google-chat");result=post(google_chat_context,member,event());assert result.status_code==200
    packet=sdk("render",{"message":result.json()["data"]["botMessage"],"destination":{"spaceName":FIXTURE["connection"]["spaceName"],"threadName":"spaces/QA_SPACE/threads/QA_THREAD"}})
    assert packet["parent"]==FIXTURE["connection"]["spaceName"]
    assert packet["body"]["text"]=="Google Chat 한국어 답변\n둘째 줄"
    assert packet["body"]["thread"]["name"]=="spaces/QA_SPACE/threads/QA_THREAD"
    assert packet["messageReplyOption"]=="REPLY_MESSAGE_OR_FAIL"
    action=packet["body"]["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert action["function"]=="cga.select"
    assert {item["key"] for item in action["parameters"]}=={"sourceMessageId","choiceIndex"}


def click_event(source_id="00000000-0000-4000-8000-000000000001",index="0",time="2026-10-31T00:00:02.000001Z",user="users/10000010"):
    native=event();native.update(type="CARD_CLICKED",eventTime=time,user={"name":user,"type":"HUMAN"},action={"actionMethodName":"cga.select","parameters":[{"key":"sourceMessageId","value":source_id},{"key":"choiceIndex","value":index}]})
    native["message"]["sender"]={"name":"users/10000999","type":"BOT"}
    return native


def test_native_mentions_commands_self_and_general():
    parsed=normalized(event());assert parsed["addressed"] and parsed["message"]=="질문" and len(parsed["eventId"])<=200
    raw=event();raw["message"]["annotations"]=[];assert not normalized(raw)["addressed"]
    other=event();other["message"]["annotations"][0]["userMention"]["user"]["name"]="users/10000998";assert not normalized(other)["addressed"]
    own=event();own["message"]["sender"]={"name":"users/10000999","type":"BOT"};assert normalized(own) is None
    for kind in ["ADDED_TO_SPACE","REMOVED_FROM_SPACE","APP_COMMAND"]:
        ignored=event();ignored["type"]=kind;assert normalized(ignored) is None
    for text,expected in [("/help","/help"),("/ask 상세 설명","상세 설명")]:
        native=event(text);native["message"]["annotations"]=[{"type":"SLASH_COMMAND","startIndex":0,"length":len(text.split()[0]),"slashCommand":{"bot":{"name":"users/10000999","type":"BOT"},"type":"INVOKE","commandName":text.split()[0]}}]
        parsed=normalized(native);assert parsed["addressed"] and parsed["message"]==expected


def test_direct_group_and_thread_scopes_remain_distinct():
    first=normalized(event());topic=event();topic["message"]["thread"]["name"]="spaces/QA_SPACE/threads/OTHER";second=normalized(topic)
    flat=event();flat["message"].pop("thread");third=normalized(flat)
    direct=event("개인 질문");direct["space"]["spaceType"]="DIRECT_MESSAGE";direct["message"]["space"]["spaceType"]="DIRECT_MESSAGE"
    fourth=normalized(direct,{**FIXTURE["connection"],"spaceType":"DIRECT_MESSAGE"})
    assert fourth["roomMode"]=="private" and fourth["addressed"]
    assert len({first["nativeRoomKey"],second["nativeRoomKey"],third["nativeRoomKey"],fourth["nativeRoomKey"]})==4
    otherbot=normalized(event(),{**FIXTURE["connection"],"botUserName":"users/10000998"})
    assert otherbot["nativeRoomKey"]!=first["nativeRoomKey"] and not otherbot["addressed"]


@pytest.mark.parametrize("problem",["space","message-space","thread-space","missing-thread","actor","sender-kind","space-kind","bad-annotation","media"])
def test_native_ambiguous_scope_is_not_accepted(problem):
    native=event()
    if problem=="space":native["space"]["name"]="spaces/OTHER"
    elif problem=="message-space":native["message"]["name"]="spaces/OTHER/messages/one"
    elif problem=="thread-space":native["message"]["thread"]["name"]="spaces/OTHER/threads/one"
    elif problem=="missing-thread":native["message"]["thread"]={"threadKey":"cannot-infer"}
    elif problem=="actor":native["user"]["name"]="users/10000011"
    elif problem=="sender-kind":native["message"]["sender"]["type"]="UNKNOWN"
    elif problem=="space-kind":native["space"]["spaceType"]="DIRECT_MESSAGE"
    elif problem=="bad-annotation":native["message"]["annotations"][0]["length"]=99999
    elif problem=="media":native["message"].pop("text");native["message"]["attachment"]=[{}]
    with pytest.raises(ValueError):normalized(native)


def test_click_hash_is_stable_and_distinguishes_interactions():
    first=normalized(click_event());assert first["sourceMessageId"]=="00000000-0000-4000-8000-000000000001" and first["choiceIndex"]==0
    assert normalized(click_event())["eventId"]==first["eventId"]
    assert normalized(click_event(time="2026-10-31T00:00:03Z"))["eventId"]!=first["eventId"]
    assert normalized(click_event(user="users/10000011"))["eventId"]!=first["eventId"]
    for native in [click_event(index="-1"),click_event(time=""),click_event(source_id="bad")]:
        with pytest.raises(ValueError):normalized(native)


@pytest.mark.parametrize("kind",["text","button","card","table","link-button","carousel"])
def test_real_talk_six_native_packets(kind):
    from app.api.routes import channels
    config={"messageType":kind,"basicMessages":["한국어 본문\n둘째 줄"],"responseType":"single-select"};variables={}
    if kind=="button":config["messages"]=["선택"]
    elif kind=="card":config["messages"]=["제목","","설명"]
    elif kind=="link-button":config["linkButtonItems"]=[{"label":"문서","url":"https://example.com/한국어"}]
    elif kind=="table":config.update(tableVariableItemId="rows",tableColumnMappings=[{"column":"name"},{"column":"id"}],tableKeyColumn="id");variables={"__items":{"rows":[{"name":"표시","id":"원본 키"}]}}
    elif kind=="carousel":config["messages"]=["목록","","항목","내용","선택","원본 키","Button","하단","하단 값"]
    output=channels._talk_output({"id":"followup","config":{"channelTemplates":{"GOOGLE_CHAT":config}}},variables,channel_code="GOOGLE_CHAT")
    packet=sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001","messageType":output["type"],"text":output["text"],"payload":{**output["payload"],"options":output["options"]}},"destination":{"spaceName":"spaces/QA_SPACE","threadName":"spaces/QA_SPACE/threads/QA_THREAD"}})
    assert packet["body"]["thread"]["name"]=="spaces/QA_SPACE/threads/QA_THREAD"
    assert "한국어 본문" in packet["body"]["text"]
    serialized=json.dumps(packet,ensure_ascii=False)
    if kind=="table":assert "원본 키" in serialized
    if kind in {"button","table","carousel"}:assert 'cga.select' in serialized and 'sourceMessageId' in serialized and 'choiceIndex' in serialized
    if kind=="link-button":assert "https://example.com/한국어" in serialized
    if kind=="card":assert "설명" in serialized


@pytest.mark.parametrize("kind",["html","form","form-a-card","dtmf"])
def test_unsupported_google_chat_templates_are_explicit(kind):
    assert admin._template_renderer_issues(channel_code="GOOGLE_CHAT",renderer_type=kind)
    with pytest.raises(ValueError):sdk("render",{"message":{"id":"known","messageType":kind,"text":"본문","payload":{}},"destination":{"spaceName":"spaces/QA_SPACE"}})


@pytest.mark.parametrize("kind,payload",[("button",{"options":[]}),("link-button",{"options":[]}),("table",{"columns":["name"],"rows":[]}),("card",{"card":{"title":"","description":"","imageUrl":""}}),("carousel",{"carousel":{}})],ids=["button","link","zero-rows","card","carousel"])
def test_empty_optional_result_preserves_text(kind,payload):
    packet=sdk("render",{"message":{"id":"known","messageType":kind,"text":"유효한 본문","payload":payload},"destination":{"spaceName":"spaces/QA_SPACE"}})
    assert packet["body"]["text"]=="유효한 본문"


@pytest.mark.parametrize("case",["byte-limit","widget-limit","url","missing-options","bad-table"])
def test_native_limits_and_structures_are_not_silently_cut(case):
    message={"id":"00000000-0000-4000-8000-000000000001","messageType":"text","text":"본문","payload":{}}
    if case=="byte-limit":message["text"]="가"*11000
    elif case=="widget-limit":message.update(messageType="table",payload={"columns":["name"],"rows":[{"name":"선택"}]*101})
    elif case=="url":message.update(messageType="card",payload={"card":{"imageUrl":"javascript:alert(1)"}})
    elif case=="missing-options":message.update(messageType="button",payload={})
    elif case=="bad-table":message.update(messageType="table",payload={"columns":["name"],"rows":["bad"]})
    with pytest.raises(ValueError):sdk("render",{"message":message,"destination":{"spaceName":"spaces/QA_SPACE"}})

def test_direct_bot_questioner_reuses_existing_api(google_chat_context):
    member=create(google_chat_context,"google-chat",kind="bot")
    response=send(google_chat_context,member,"API 봇 질문","google-chat",addressed=True,event="google-chat-direct-bot-1")
    assert response.status_code==200 and response.json()["data"]["userMessage"]["participantKind"]=="bot"


def test_completed_callback_update_restores_and_new_stale_is_rejected(google_chat_context):
    c,_=google_chat_context;member=create(google_chat_context,"google-chat");message=post(google_chat_context,member,FIXTURE["addressed"]).json()["data"]["botMessage"]
    wire=json.dumps({"webchatRichFormVersion":"1.0","sourceMessageId":message["id"],"sourceTalkNodeId":"followup","response":{"buttonValue":"추가 설명"}},ensure_ascii=False)
    answer=send(google_chat_context,member,wire,"google-chat",addressed=True,event="google-chat-fixture:10005");assert answer.status_code==200
    repeated=send(google_chat_context,member,wire,"google-chat",addressed=True,event="google-chat-fixture:10005")
    assert repeated.status_code==200 and repeated.json()["data"]["botMessage"]["id"]==answer.json()["data"]["botMessage"]["id"] and len(c[3])==1
    assert send(google_chat_context,member,wire,"google-chat",addressed=True,event="google-chat-fixture:10006").status_code==409 and len(c[3])==1
