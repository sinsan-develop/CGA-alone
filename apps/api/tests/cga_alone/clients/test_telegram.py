"""S10 native 표본→기존 질문 API/PG/Talk→Telegram packet. 실 플랫폼/Provider 없음."""
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
FIXTURE = json.loads((Path(__file__).parents[1]/"fixtures/client_templates/telegram.json").read_text(encoding="utf-8"))


def sdk(operation, value):
    code = r"""
const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(p.root+'/apps/web/node_modules/typescript');
const source=fs.readFileSync(p.root+'/packages/shared/src/channel.ts','utf8'),holder={exports:{}};
new Function('exports','module','process',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(holder.exports,holder,process);
const fn=holder.exports[{normalize:'normalizeAidotTelegramEvent',render:'buildAidotTelegramReplyPayload'}[p.operation]];
if(!fn)console.log(JSON.stringify({missing:p.operation}));else try{const v=p.value;console.log(JSON.stringify({result:p.operation==='normalize'?fn(v.event,v.connection):fn(v.message,v.destination)}));}catch(e){console.log(JSON.stringify({error:e.message}));}
"""
    result=subprocess.run(["node","-e",code],input=json.dumps({"root":str(ROOT),"operation":operation,"value":value},ensure_ascii=False),text=True,capture_output=True,encoding="utf-8")
    assert result.returncode == 0, result.stderr
    data=json.loads(result.stdout);assert "missing" not in data, data
    if "error" in data:raise ValueError(data["error"])
    return data["result"]


def event(text=None,user=None,message_id=None,channel=None):
    item=deepcopy(FIXTURE["addressed"])
    if text is not None:
        item["message"]["text"]=text
        item["message"]["entities"]=[{"type":"mention","offset":0,"length":12}] if text.startswith("@CgaAloneBot") else []
    if user is not None:item["message"]["from"]["id"]=user
    if message_id is not None:item["message"]["message_id"]=message_id;item["update_id"]=10000+message_id
    if channel is not None:item["message"]["chat"]["id"]=channel
    return item


def normalized(native, connection=None):return sdk("normalize",{"event":native,"connection":connection or FIXTURE["connection"]})


@pytest.fixture
def telegram_context(client_context):
    client_context[0][2].add(AdminChannel(organization_id=client_context[0][0].organization_id,code="TELEGRAM",name="S10 Telegram QA",status="active"))
    client_context[0][2].flush()
    return client_context


def post(context, member, native, connection=None):
    parsed=normalized(native,connection)
    return send(context,member,parsed["message"],"telegram",addressed=parsed["addressed"],event=parsed["eventId"])


def test_addressed_message_invokes_bot(telegram_context):
    c,_=telegram_context;member=create(telegram_context,"telegram")
    result=post(telegram_context,member,event());assert result.status_code==200
    assert result.json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==1
    assert post(telegram_context,member,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    assert len(c[3])==1


def test_general_message_does_not_invoke_bot(telegram_context):
    c,_=telegram_context;member=create(telegram_context,"telegram")
    result=post(telegram_context,member,FIXTURE["general"]);assert result.status_code==200 and result.json()["data"]["botMessages"]==[]
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==0


def test_other_actor_cannot_continue(telegram_context):
    c,client=telegram_context;first=create(telegram_context,"telegram")
    assert post(telegram_context,first,event()).status_code==200
    joined=client.post(f"/api/v1/channels/telegram/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":"B"});assert joined.status_code==200
    second={**joined.json()["data"],"room":first["room"]};room=c[2].get(ChannelRoom,UUID(first["roomId"]));before=deepcopy(room.metadata_json.get("variables"))
    assert post(telegram_context,second,event(user=10000011,message_id=104)).status_code==409
    c[2].expire_all();assert room.metadata_json.get("variables")==before and not c[3]
    assert post(telegram_context,second,FIXTURE["general"]).status_code==200


def test_duplicate_event_runs_once(telegram_context):
    c,_=telegram_context;member=create(telegram_context,"telegram")
    first=post(telegram_context,member,event());assert first.status_code==200
    repeated=post(telegram_context,member,event());assert repeated.status_code==200
    assert repeated.json()["data"]["userMessage"]["id"]==first.json()["data"]["userMessage"]["id"]
    answer=event("@CgaAloneBot 추가 설명",message_id=103)
    completed=post(telegram_context,member,answer);assert completed.status_code==200
    assert post(telegram_context,member,answer).json()["data"]["botMessage"]["id"]==completed.json()["data"]["botMessage"]["id"]
    assert len(c[3])==1
    assert post(telegram_context,member,event("바뀐 내용",message_id=103)).status_code==409


def test_same_actor_in_different_rooms_is_isolated(telegram_context):
    c,client=telegram_context;first=create(telegram_context,"telegram");second=create(telegram_context,"telegram")
    other=event(channel=-10000002);connection={**FIXTURE["connection"],"chatId":"-10000002"}
    assert normalized(event())["nativeRoomKey"]!=normalized(other,connection)["nativeRoomKey"]
    assert post(telegram_context,first,event()).status_code==200 and post(telegram_context,second,other,connection).status_code==200
    assert post(telegram_context,first,event("@CgaAloneBot 추가 설명",message_id=103)).status_code==200
    result=client.get(f"/api/v1/channels/telegram/rooms/{second['roomId']}",headers=member_headers(second));assert result.status_code==200
    assert result.json()["data"]["runtime"]["waitingNodeId"]=="followup" and len(c[3])==1


def test_template_round_trip_to_client_payload(telegram_context,template_context):
    c,_=telegram_context;_,user=template_context;db=c[2]
    template=admin.create_admin_template(TemplateCreateRequest(channel_code="TELEGRAM",name="S10 Telegram 버튼",renderer_type="button",item_types="button"),_request(),db=db,current_user=user)["data"]
    assert any(str(item["id"])==template["id"] for item in bots.list_talk_templates(_request(),channel_code="TELEGRAM",db=db,current_user=user)["data"]["items"])
    document=deepcopy(c[1].version_json);node=next(item for item in document["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    node["config"]["channelTemplates"]={"TELEGRAM":FIXTURE["talk"]}
    bots.update_version(c[0].id,c[1].id,VersionUpdateRequest(version_json=document),_request(),current_user=user,db=db)
    db.expire_all();saved=db.get(BotVersion,c[1].id)
    assert next(item for item in saved.version_json["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")["config"]["channelTemplates"]["TELEGRAM"]==FIXTURE["talk"]
    member=create(telegram_context,"telegram");result=post(telegram_context,member,event());assert result.status_code==200
    packet=sdk("render",{"message":result.json()["data"]["botMessage"],"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":None}})
    assert packet["parameters"]["chat_id"]==FIXTURE["connection"]["chatId"] and packet["parameters"]["text"]=="텔레그램 한국어 답변\n둘째 줄"
    assert packet["method"]=="sendMessage"
    assert packet["parameters"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("cga:")



def test_private_chat_keeps_its_own_question_scope(telegram_context):
    c,client=telegram_context;private=create(telegram_context,"telegram",mode="private")
    connection={**FIXTURE["connection"],"chatId":"10000010","chatType":"private"}
    assert normalized(FIXTURE["private"],connection)["roomMode"]=="private"
    assert normalized(FIXTURE["private"],connection)["nativeRoomKey"]!=normalized(FIXTURE["addressed"])["nativeRoomKey"]
    response=post(telegram_context,private,FIXTURE["private"],connection);assert response.status_code==200
    assert response.json()["data"]["runtime"]["waitingNodeId"]=="followup"
    group=create(telegram_context,"telegram")
    denied=client.get(f"/api/v1/channels/telegram/rooms/{private['roomId']}",headers=member_headers(group));assert denied.status_code==404


def test_telegram_topics_do_not_fallback_to_main_chat():
    main=normalized(FIXTURE["addressed"]);topic=deepcopy(FIXTURE["addressed"]);topic["message"].update(message_thread_id=41,is_topic_message=True)
    selected=normalized(topic);assert selected["threadId"]==41 and selected["nativeRoomKey"]!=main["nativeRoomKey"]
    for value in [0,-1,"41",False,None]:
        invalid=deepcopy(topic);invalid["message"]["message_thread_id"]=value
        with pytest.raises(ValueError):normalized(invalid)
    missing=deepcopy(topic);missing["message"].pop("message_thread_id")
    with pytest.raises(ValueError):normalized(missing)


def test_native_mentions_commands_and_utf16_offsets():
    mention=normalized(FIXTURE["addressed"]);assert mention["addressed"] and mention["message"]=="미등록 텔레그램 질문"
    command=event("/ask@CgaAloneBot 설명");command["message"]["entities"]=[{"type":"bot_command","offset":0,"length":len("/ask@CgaAloneBot")}]
    assert normalized(command)["message"]=="설명"
    generic=event("/ask 설명");generic["message"]["entities"]=[{"type":"bot_command","offset":0,"length":4}];assert not normalized(generic)["addressed"]
    emoji=event("😀 @CgaAloneBot 질문");emoji["message"]["entities"]=[{"type":"mention","offset":3,"length":12}]
    parsed=normalized(emoji);assert parsed["message"].startswith("😀") and "@CgaAloneBot" not in parsed["message"] and parsed["addressed"]
    literal=event("@CgaAloneBot 질문");literal["message"]["entities"]=[];assert not normalized(literal)["addressed"]


def test_self_edits_and_service_updates_never_ask():
    own=event();own["message"]["from"].update(id=10000999,is_bot=True);assert normalized(own) is None
    edited={"update_id":10004,"edited_message":FIXTURE["addressed"]["message"]};assert normalized(edited) is None
    service=event();service["message"].pop("text");service["message"]["new_chat_members"]=[];assert normalized(service) is None


@pytest.mark.parametrize("problem",["chat","kind","unsafe-id","anonymous"],ids=str)
def test_unverified_native_identity_is_not_an_owner(problem):
    native=event();connection=FIXTURE["connection"]
    if problem=="chat":native["message"]["chat"]["id"]=-10000002
    elif problem=="kind":native["message"]["chat"]["type"]="private"
    elif problem=="unsafe-id":native["message"]["from"]["id"]=9007199254740992
    elif problem=="anonymous":native["message"]["sender_chat"]={"id":-10000001,"type":"supergroup"}
    with pytest.raises(ValueError):normalized(native,connection)


@pytest.mark.parametrize("kind",["text","button","card","table","link-button","carousel"])
def test_real_talk_six_native_packets(kind):
    from app.api.routes import channels
    config={"messageType":kind,"basicMessages":["한국어 본문\n둘째 줄"],"responseType":"single-select"};variables={}
    if kind=="button":config["messages"]=["선택"]
    elif kind=="card":config["messages"]=["제목","","설명"]
    elif kind=="link-button":config["linkButtonItems"]=[{"label":"문서","url":"https://example.com/한국어"}]
    elif kind=="table":
        config.update(tableVariableItemId="rows",tableColumnMappings=[{"column":"name"},{"column":"id"}],tableKeyColumn="id");variables={"__items":{"rows":[{"name":"표시","id":"원본 키"}]}}
    elif kind=="carousel":config["messages"]=["목록","","항목","내용","선택","원본 키","Button","하단","하단 값"]
    output=channels._talk_output({"id":"followup","config":{"channelTemplates":{"TELEGRAM":config}}},variables,channel_code="TELEGRAM")
    packet=sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001","messageType":output["type"],"text":output["text"],"payload":{**output["payload"],"options":output["options"]}},"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":41}})
    assert packet["method"]=="sendMessage" and packet["parameters"]["message_thread_id"]==41
    assert "한국어 본문" in packet["parameters"]["text"]
    if kind=="table":assert "원본 키" in packet["parameters"]["text"]
    if kind in {"button","table","carousel"}:
        data=packet["parameters"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert data.startswith("cga:00000000-0000-4000-8000-000000000001:") and len(data.encode("utf-8"))<=64 and "원본 키" not in data
    if kind=="link-button":assert packet["parameters"]["reply_markup"]["inline_keyboard"][0][0]["url"]=="https://example.com/한국어"
    if kind=="card":assert "설명" in packet["parameters"]["text"]


@pytest.mark.parametrize("kind",["html","form","form-a-card","dtmf"])
def test_unsupported_telegram_templates_are_explicit(kind):
    assert admin._template_renderer_issues(channel_code="TELEGRAM",renderer_type=kind)
    with pytest.raises(ValueError):sdk("render",{"message":{"id":"known","messageType":kind,"text":"본문","payload":{}},"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":None}})


@pytest.mark.parametrize("kind,payload",[("button",{"options":[]}),("link-button",{"options":[]}),("table",{"columns":["name"],"rows":[]}),("card",{"card":{"title":"","description":"","imageUrl":""}}),("carousel",{"carousel":{}})],ids=["button","link","zero-rows","card","carousel"])
def test_valid_empty_optional_payload_keeps_text(kind,payload):
    packet=sdk("render",{"message":{"id":"known","messageType":kind,"text":"유효한 본문","payload":payload},"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":None}})
    assert packet["method"]=="sendMessage" and "유효한 본문" in packet["parameters"]["text"] and not packet["parameters"].get("reply_markup")


@pytest.mark.parametrize("case",["text","caption","url","missing-options","bad-table"],ids=str)
def test_native_limits_and_invalid_structures_are_not_silently_cut(case):
    message={"id":"00000000-0000-4000-8000-000000000001","messageType":"text","text":"본문","payload":{}}
    if case=="text":message["text"]="x"*4097
    elif case=="caption":message.update(messageType="card",text="x"*1025,payload={"card":{"imageUrl":"https://example.com/image.png"}})
    elif case=="url":message.update(messageType="card",payload={"card":{"imageUrl":"javascript:alert(1)"}})
    elif case=="missing-options":message.update(messageType="button",payload={})
    elif case=="bad-table":message.update(messageType="table",payload={"columns":None,"rows":[]})
    with pytest.raises(ValueError):sdk("render",{"message":message,"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":None}})


def test_photo_packet_and_html_escape_preserve_content():
    packet=sdk("render",{"message":{"id":"known","messageType":"card","text":"본문 <&>","payload":{"card":{"title":"제목","description":"설명","imageUrl":"https://example.com/image.png"}}},"destination":{"chatId":FIXTURE["connection"]["chatId"],"threadId":None}})
    assert packet["method"]=="sendPhoto" and packet["parameters"]["photo"]=="https://example.com/image.png" and packet["parameters"]["parse_mode"]=="HTML"
    assert "&lt;&amp;&gt;" in packet["parameters"]["caption"] and "제목" in packet["parameters"]["caption"] and "설명" in packet["parameters"]["caption"]


def test_direct_bot_questioner_reuses_existing_api(telegram_context):
    member=create(telegram_context,"telegram",kind="bot")
    response=send(telegram_context,member,"API 봇 질문","telegram",addressed=True,event="telegram-direct-bot-1")
    assert response.status_code==200 and response.json()["data"]["userMessage"]["participantKind"]=="bot"


def test_completed_callback_update_restores_and_new_stale_is_rejected(telegram_context):
    c,_=telegram_context;member=create(telegram_context,"telegram");message=post(telegram_context,member,FIXTURE["addressed"]).json()["data"]["botMessage"]
    wire=json.dumps({"webchatRichFormVersion":"1.0","sourceMessageId":message["id"],"sourceTalkNodeId":"followup","response":{"buttonValue":"추가 설명"}},ensure_ascii=False)
    answer=send(telegram_context,member,wire,"telegram",addressed=True,event="telegram-update:10005");assert answer.status_code==200
    repeated=send(telegram_context,member,wire,"telegram",addressed=True,event="telegram-update:10005")
    assert repeated.status_code==200 and repeated.json()["data"]["botMessage"]["id"]==answer.json()["data"]["botMessage"]["id"] and len(c[3])==1
    assert send(telegram_context,member,wire,"telegram",addressed=True,event="telegram-update:10006").status_code==409 and len(c[3])==1
