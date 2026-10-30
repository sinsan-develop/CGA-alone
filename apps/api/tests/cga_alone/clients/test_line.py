"""S12 native 표본→기존 질문 API/PG/Talk→LINE packet. 실 플랫폼/Provider 없음."""
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
FIXTURE = json.loads((Path(__file__).parents[1]/"fixtures/client_templates/line.json").read_text(encoding="utf-8"))


def sdk(operation, value):
    code = r"""
const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(p.root+'/apps/web/node_modules/typescript');
const source=fs.readFileSync(p.root+'/packages/shared/src/channel.ts','utf8'),holder={exports:{}};
new Function('exports','module','process',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(holder.exports,holder,process);
const fn=holder.exports[{normalize:'normalizeAidotLineEvent',render:'buildAidotLineMessage'}[p.operation]];
if(!fn)console.log(JSON.stringify({missing:p.operation}));else (async()=>{try{const v=p.value;console.log(JSON.stringify({result:await (p.operation==='normalize'?fn(v.event,v.connection):fn(v.message,v.destination))}));}catch(e){console.log(JSON.stringify({error:e.message}));}})();
"""
    result=subprocess.run(["node","-e",code],input=json.dumps({"root":str(ROOT),"operation":operation,"value":value},ensure_ascii=False),text=True,capture_output=True,encoding="utf-8")
    assert result.returncode == 0, result.stderr
    data=json.loads(result.stdout);assert "missing" not in data, data
    if "error" in data:raise ValueError(data["error"])
    return data["result"]


def event(text=None,user=None,message_id=None,channel=None):
    item=deepcopy(FIXTURE["addressed"]);native=item["events"][0]
    if text is not None:
        native["message"]["text"]=text
        if not text.startswith("@CgaAloneBot"):native["message"].pop("mention",None)
    if user is not None:native["source"]["userId"]=user
    if message_id is not None:
        native["message"]["id"]=str(message_id)
        native["webhookEventId"]=f"01K8XY2Z{message_id:018d}"
    if channel is not None:native["source"]["groupId"]=channel
    return item


def normalized(native, connection=None):return sdk("normalize",{"event":native,"connection":connection or FIXTURE["connection"]})


@pytest.fixture
def line_context(client_context):
    client_context[0][2].add(AdminChannel(organization_id=client_context[0][0].organization_id,code="LINE",name="S12 Line QA",status="active"))
    client_context[0][2].flush()
    return client_context


def post(context, member, native, connection=None):
    parsed=normalized(native,connection)
    return send(context,member,parsed["message"],"line",addressed=parsed["addressed"],event=parsed["eventId"])


def test_addressed_message_invokes_bot(line_context):
    c,_=line_context;member=create(line_context,"line")
    result=post(line_context,member,event());assert result.status_code==200
    assert result.json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==1
    assert post(line_context,member,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    assert len(c[3])==1


def test_general_message_does_not_invoke_bot(line_context):
    c,_=line_context;member=create(line_context,"line")
    result=post(line_context,member,FIXTURE["general"]);assert result.status_code==200 and result.json()["data"]["botMessages"]==[]
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==0


def test_other_actor_cannot_continue(line_context):
    c,client=line_context;first=create(line_context,"line")
    assert post(line_context,first,event()).status_code==200
    joined=client.post(f"/api/v1/channels/line/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":"B"});assert joined.status_code==200
    second={**joined.json()["data"],"room":first["room"]};room=c[2].get(ChannelRoom,UUID(first["roomId"]));before=deepcopy(room.metadata_json.get("variables"))
    assert post(line_context,second,event(user="U"+"b"*32,message_id=104)).status_code==409
    c[2].expire_all();assert room.metadata_json.get("variables")==before and not c[3]
    assert post(line_context,second,FIXTURE["general"]).status_code==200


def test_duplicate_event_runs_once(line_context):
    c,_=line_context;member=create(line_context,"line")
    first=post(line_context,member,event());assert first.status_code==200
    repeated=post(line_context,member,event());assert repeated.status_code==200
    assert repeated.json()["data"]["userMessage"]["id"]==first.json()["data"]["userMessage"]["id"]
    answer=event("@CgaAloneBot 추가 설명",message_id=103)
    completed=post(line_context,member,answer);assert completed.status_code==200
    assert post(line_context,member,answer).json()["data"]["botMessage"]["id"]==completed.json()["data"]["botMessage"]["id"]
    assert len(c[3])==1
    assert post(line_context,member,event("바뀐 내용",message_id=103)).status_code==409


def test_same_actor_in_different_rooms_is_isolated(line_context):
    c,client=line_context;first=create(line_context,"line");second=create(line_context,"line")
    other=event(channel="C"+"2"*32);connection={**FIXTURE["connection"],"sourceId":"C"+"2"*32}
    assert normalized(event())["nativeRoomKey"]!=normalized(other,connection)["nativeRoomKey"]
    assert post(line_context,first,event()).status_code==200 and post(line_context,second,other,connection).status_code==200
    assert post(line_context,first,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    result=client.get(f"/api/v1/channels/line/rooms/{second['roomId']}",headers=member_headers(second));assert result.status_code==200
    assert result.json()["data"]["runtime"]["waitingNodeId"]=="followup" and len(c[3])==1


def test_template_round_trip_to_client_payload(line_context,template_context):
    c,_=line_context;_,user=template_context;db=c[2]
    template=admin.create_admin_template(TemplateCreateRequest(channel_code="LINE",name="S12 LINE 버튼",renderer_type="button",item_types="button"),_request(),db=db,current_user=user)["data"]
    assert any(str(item["id"])==template["id"] for item in bots.list_talk_templates(_request(),channel_code="LINE",db=db,current_user=user)["data"]["items"])
    document=deepcopy(c[1].version_json);node=next(item for item in document["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    node["config"]["channelTemplates"]={"LINE":FIXTURE["talk"]}
    bots.update_version(c[0].id,c[1].id,VersionUpdateRequest(version_json=document),_request(),current_user=user,db=db)
    db.expire_all();saved=db.get(BotVersion,c[1].id)
    assert next(item for item in saved.version_json["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")["config"]["channelTemplates"]["LINE"]==FIXTURE["talk"]
    member=create(line_context,"line");result=post(line_context,member,event());assert result.status_code==200
    packet=sdk("render",{"message":result.json()["data"]["botMessage"]})
    assert packet["type"]=="flex" and "LINE 한국어 답변\n둘째 줄" in json.dumps(packet,ensure_ascii=False).replace("\\n","\n")
    contents=packet["contents"]
    assert contents["type"]=="bubble"
    button=contents["footer"]["contents"][0]
    assert button["type"]=="button" and button["action"]["type"]=="postback"
    assert button["action"]["data"].startswith("cga:") and len(button["action"]["data"])<=300


@pytest.mark.parametrize("kind",["text","button","card","table","link-button","carousel"])
def test_real_talk_six_native_packets(kind):
    from app.api.routes import channels
    config={"messageType":kind,"basicMessages":["한국어 본문\n둘째 줄"],"responseType":"single-select"};variables={}
    if kind=="button":config["messages"]=["선택"]
    elif kind=="card":config["messages"]=["제목","","설명"]
    elif kind=="link-button":config["linkButtonItems"]=[{"label":"문서","url":"https://example.com/한국어"}]
    elif kind=="table":config.update(tableVariableItemId="rows",tableColumnMappings=[{"column":"name"},{"column":"id"}],tableKeyColumn="id");variables={"__items":{"rows":[{"name":"표시","id":"원본 키"}]}}
    elif kind=="carousel":config["messages"]=["목록","","항목","내용","선택","원본 키","Button","하단","하단 값"]
    output=channels._talk_output({"id":"followup","config":{"channelTemplates":{"LINE":config}}},variables,channel_code="LINE")
    packet=sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001","messageType":output["type"],"text":output["text"],"payload":{**output["payload"],"options":output["options"]}},"destination":{"spaceName":"spaces/QA_SPACE","threadName":"spaces/QA_SPACE/threads/QA_THREAD"}})
    assert packet["type"] in {"text","flex"}
    assert "한국어 본문" in json.dumps(packet,ensure_ascii=False)
    serialized=json.dumps(packet,ensure_ascii=False)
    if kind=="table":assert "원본 키" in serialized
    if kind in {"button","table","carousel"}:assert 'cga:00000000-0000-4000-8000-000000000001:' in serialized
    if kind=="link-button":assert "https://example.com/%ED%95%9C%EA%B5%AD%EC%96%B4" in serialized
    if kind=="card":assert "설명" in serialized


@pytest.mark.parametrize("kind",["html","form","form-a-card","dtmf"])
def test_unsupported_line_templates_are_explicit(kind):
    assert admin._template_renderer_issues(channel_code="LINE",renderer_type=kind)
    with pytest.raises(ValueError):sdk("render",{"message":{"id":"known","messageType":kind,"text":"본문","payload":{}},"destination":{"spaceName":"spaces/QA_SPACE"}})


@pytest.mark.parametrize("kind,payload",[("button",{"options":[]}),("link-button",{"options":[]}),("table",{"columns":["name"],"rows":[]}),("card",{"card":{"title":"","description":"","imageUrl":""}}),("carousel",{"carousel":{}})],ids=["button","link","zero-rows","card","carousel"])
def test_empty_optional_result_preserves_text(kind,payload):
    packet=sdk("render",{"message":{"id":"known","messageType":kind,"text":"유효한 본문","payload":payload},"destination":{"spaceName":"spaces/QA_SPACE"}})
    assert "유효한 본문" in json.dumps(packet,ensure_ascii=False)




def test_native_scopes_mentions_self_and_ignored_events():
    first=normalized(event());assert first["message"]=="질문" and first["addressed"]
    assert not normalized(FIXTURE["general"])["addressed"]
    direct=event("개인 질문");direct["events"][0]["source"]={"type":"user","userId":"U"+"a"*32}
    one=normalized(direct,{**FIXTURE["connection"],"sourceType":"user","sourceId":"U"+"a"*32});assert one["roomMode"]=="private" and one["addressed"]
    multi=event();multi["events"][0]["source"]={"type":"room","roomId":"R"+"1"*32,"userId":"U"+"a"*32}
    two=normalized(multi,{**FIXTURE["connection"],"sourceType":"room","sourceId":"R"+"1"*32});assert two["roomMode"]=="shared"
    assert len({first["nativeRoomKey"],one["nativeRoomKey"],two["nativeRoomKey"]})==3
    assert normalized(event(user=FIXTURE["connection"]["botUserId"])) is None
    for kind in ["unsend","join","follow"]:
        item=event();item["events"][0]["type"]=kind;assert normalized(item) is None
    item=event();item["events"][0]["message"]={"type":"image","id":"123"};assert normalized(item) is None


@pytest.mark.parametrize("problem",["destination","group","actor","mention-range","mention-self","mention-list","event-id","batch"])
def test_native_ambiguous_input_is_rejected(problem):
    item=event();native=item["events"][0]
    if problem=="destination":item["destination"]="U"+"8"*32
    elif problem=="group":native["source"]["groupId"]="C"+"8"*32
    elif problem=="actor":native["source"].pop("userId")
    elif problem=="mention-range":native["message"]["mention"]["mentionees"][0]["length"]=999
    elif problem=="mention-self":native["message"]["mention"]["mentionees"][0]["userId"]="U"+"8"*32
    elif problem=="mention-list":native["message"]["mention"]["mentionees"]={}
    elif problem=="event-id":native["webhookEventId"]=""
    else:item["events"].append(deepcopy(native))
    with pytest.raises(ValueError):normalized(item)


def test_redelivery_keeps_same_dedup_id():
    original=normalized(event());redelivered=event();redelivered["events"][0]["deliveryContext"]["isRedelivery"]=True;redelivered["events"][0]["replyToken"]="different-token"
    assert normalized(redelivered)["eventId"]==original["eventId"]


@pytest.mark.parametrize("case",["utf16","label","url","bubble","missing-options","bad-table"])
def test_native_bounds_reject_without_truncation(case):
    message={"id":"00000000-0000-4000-8000-000000000001","messageType":"text","text":"본문","payload":{}}
    if case=="utf16":message["text"]="😀"*2501
    elif case=="label":message.update(messageType="button",payload={"options":["가"*41]})
    elif case=="url":message.update(messageType="card",payload={"card":{"imageUrl":"http://example.com/image.png"}})
    elif case=="bubble":message.update(messageType="card",payload={"card":{"description":"가"*11000}})
    elif case=="missing-options":message.update(messageType="button",payload={})
    else:message.update(messageType="table",payload={"columns":["a"],"rows":["bad"]})
    with pytest.raises(ValueError):sdk("render",{"message":message})


def test_direct_bot_questioner_reuses_existing_api(line_context):
    member=create(line_context,"line",kind="bot")
    response=send(line_context,member,"API 봇 질문","line",addressed=True,event="line-direct-bot-1")
    assert response.status_code==200 and response.json()["data"]["userMessage"]["participantKind"]=="bot"


def test_completed_callback_restores_and_new_stale_is_rejected(line_context):
    c,_=line_context;member=create(line_context,"line");message=post(line_context,member,FIXTURE["addressed"]).json()["data"]["botMessage"]
    wire=json.dumps({"webchatRichFormVersion":"1.0","sourceMessageId":message["id"],"sourceTalkNodeId":"followup","response":{"buttonValue":"추가 설명"}},ensure_ascii=False)
    answer=send(line_context,member,wire,"line",addressed=True,event="line-fixture:10005");assert answer.status_code==200
    repeated=send(line_context,member,wire,"line",addressed=True,event="line-fixture:10005")
    assert repeated.status_code==200 and repeated.json()["data"]["botMessage"]["id"]==answer.json()["data"]["botMessage"]["id"] and len(c[3])==1
    assert send(line_context,member,wire,"line",addressed=True,event="line-fixture:10006").status_code==409 and len(c[3])==1
