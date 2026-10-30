"""S08 Slack 표본→기존 공통 API→실 PG→native SDK. 실제 Slack 게시/Provider 없음."""
import json
import os
from pathlib import Path
from copy import deepcopy
from uuid import UUID
import subprocess
from urllib.parse import parse_qs, urlparse
import pytest
from sqlalchemy import select, func
from fastapi import HTTPException
from app.api.routes import bots, admin
from app.models import AdminChannel, ChannelRoom, ChannelMessage, ChannelQueueEvent, BotVersion
from app.schemas.admin import TemplateCreateRequest
from app.schemas.bot import VersionUpdateRequest
from tests.cga_alone.test_client_contract import client_context, create, send, member_headers
from tests.cga_alone.test_client_templates import template_context
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.integration.test_bot_persistence_regression import _request

ROOT = Path(__file__).resolve().parents[5]
FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/client_templates/slack.json").read_text(encoding="utf-8"))


def sdk(operation, value):
    script = r"""
const fs=require('node:fs');const input=JSON.parse(fs.readFileSync(0,'utf8'));const root=input.root;
const ts=require(root+'/apps/web/node_modules/typescript');
const source=fs.readFileSync(root+'/packages/shared/src/channel.ts','utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const holder={exports:{}};new Function('exports','module','process',compiled)(holder.exports,holder,process);
const names={normalize:'normalizeAidotSlackEvent',render:'buildAidotSlackReplyPayload'};const fn=holder.exports[names[input.operation]];
if(typeof fn!=='function'){console.log(JSON.stringify({missing:names[input.operation]}));}else{
 try{const v=input.value;console.log(JSON.stringify({result:input.operation==='normalize'?fn(v.event,v.connection):fn(v.message,v.destination)}));}
 catch(e){console.log(JSON.stringify({sdkError:e.message}));}
}
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"root":str(ROOT),"operation":operation,"value":value},ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", env={**os.environ,"AIDOT_WEBCHAT_API_KEY":"s08-sdk-fixture-only"})
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert "missing" not in parsed, "승인된 Slack native SDK helper가 필요합니다: " + str(parsed.get("missing"))
    if "sdkError" in parsed: raise ValueError(parsed["sdkError"])
    return parsed["result"]


def normalize(event): return sdk("normalize", {"event":event,"connection":FIXTURE["connection"]})


def event(text="<@UQA999> 미등록 슬랙 질문", user="UQA001", ts="1793376000.000100", thread=None):
    item=deepcopy(FIXTURE["addressed"]);item["event"].update(text=text,user=user,ts=ts)
    if thread is not None:item["event"]["thread_ts"]=thread
    return item


@pytest.fixture
def slack_context(client_context):
    client_context[0][2].add(AdminChannel(organization_id=client_context[0][0].organization_id,code="SLACK",name="S08 Slack QA",status="active"))
    client_context[0][2].flush()
    return client_context


def room(context, native=None, mode="shared", kind="user"):
    result=create(context,"slack",mode,kind)
    normalized=normalize(native or event())
    return {**result,"nativeRoomKey":normalized["nativeRoomKey"],"nativeParticipantKey":normalized["nativeParticipantKey"]}


def post(context, member, native):
    normalized=normalize(native)
    assert normalized["nativeRoomKey"]==member["nativeRoomKey"]
    assert normalized["nativeParticipantKey"]==member["nativeParticipantKey"]
    return send(context,member,normalized["message"],"slack",addressed=normalized["addressed"],event=normalized["eventId"])


def test_addressed_message_invokes_bot(slack_context):
    c,_=slack_context;member=room(slack_context)
    response=post(slack_context,member,event())
    assert response.status_code==200
    assert response.json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==1
    assert post(slack_context,member,event("<@UQA999> 추가 설명",ts="1793376000.000101")).status_code==200
    assert len(c[3])==1 and parse_qs(urlparse(c[3][0]).query)["q"]==["미등록 슬랙 질문"]


def test_general_message_does_not_invoke_bot(slack_context):
    c,_=slack_context;member=room(slack_context,FIXTURE["general"])
    response=post(slack_context,member,FIXTURE["general"])
    assert response.status_code==200 and response.json()["data"]["botMessages"]==[]
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==0


def test_other_actor_cannot_continue(slack_context):
    c,client=slack_context;first=room(slack_context)
    assert post(slack_context,first,event()).status_code==200
    joined=client.post(f"/api/v1/channels/slack/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":"B"})
    assert joined.status_code==200
    second={**joined.json()["data"],"room":first["room"],"nativeRoomKey":first["nativeRoomKey"],"nativeParticipantKey":normalize(event(user="UQA002"))["nativeParticipantKey"]}
    stored=c[2].get(ChannelRoom,UUID(first["roomId"]));before=deepcopy(stored.metadata_json.get("variables"))
    assert post(slack_context,second,event(user="UQA002",ts="1793376000.000200")).status_code==409
    assert stored.metadata_json.get("variables")==before and not c[3]
    general=deepcopy(FIXTURE["general"]);general["event"]["ts"]="1793376000.000201";general["event_id"]="EvQA003"
    assert post(slack_context,second,general).status_code==200


def test_duplicate_event_runs_once(slack_context):
    c,_=slack_context;member=room(slack_context)
    first=post(slack_context,member,event());assert first.status_code==200
    duplicate_event=event();duplicate_event["event_id"]="EvOtherNotice";duplicate_event["event"]["type"]="message"
    repeated=post(slack_context,member,duplicate_event)
    assert repeated.status_code==200 and repeated.json()["data"]["userMessage"]["id"]==first.json()["data"]["userMessage"]["id"]
    followup=event("<@UQA999> 추가 설명",ts="1793376000.000101")
    answer=post(slack_context,member,followup);assert answer.status_code==200 and len(c[3])==1
    assert post(slack_context,member,followup).json()["data"]["botMessage"]["id"]==answer.json()["data"]["botMessage"]["id"]
    assert len(c[3])==1
    assert post(slack_context,member,event("<@UQA999> 바뀐 내용",ts="1793376000.000101")).status_code==409


def test_same_actor_in_different_rooms_is_isolated(slack_context):
    c,client=slack_context;one=event(thread="1793300000.000001");two=event(thread="1793300000.000002")
    first=room(slack_context,one);second=room(slack_context,two)
    assert first["nativeRoomKey"]!=second["nativeRoomKey"] and first["roomId"]!=second["roomId"]
    assert post(slack_context,first,one).status_code==200 and post(slack_context,second,two).status_code==200
    followup=event("<@UQA999> A 설명",ts="1793376000.000102",thread="1793300000.000001")
    assert post(slack_context,first,followup).status_code==200
    response=client.get(f"/api/v1/channels/slack/rooms/{second['roomId']}",headers=member_headers(second))
    assert response.status_code==200 and response.json()["data"]["runtime"]["waitingNodeId"]=="followup"
    assert len(c[3])==1


def test_template_round_trip_to_client_payload(slack_context,template_context):
    c,_=slack_context;_,user=template_context;db=c[2]
    created=admin.create_admin_template(TemplateCreateRequest(channel_code="SLACK",name="S08 Slack 버튼",renderer_type="button",item_types="text,buttons"),_request(),db=db,current_user=user)
    template_id=created["data"]["id"]
    listed=bots.list_talk_templates(_request(),channel_code="SLACK",db=db,current_user=user)["data"]["items"]
    assert any(str(item["id"])==template_id and item["renderer_type"]=="button" for item in listed)
    document=deepcopy(c[1].version_json)
    node=next(item for item in document["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    node["config"]["channelTemplates"]={"SLACK":FIXTURE["talk"]}
    bots.update_version(c[0].id,c[1].id,VersionUpdateRequest(version_json=document),_request(),current_user=user,db=db)
    db.expire_all();version=db.get(BotVersion,c[1].id)
    saved=next(item for item in version.version_json["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    assert saved["config"]["channelTemplates"]["SLACK"]==FIXTURE["talk"]
    member=room(slack_context);response=post(slack_context,member,event());assert response.status_code==200
    message=response.json()["data"]["botMessage"]
    payload=sdk("render",{"message":message,"destination":{"channelId":"CQA001","threadTs":"1793300000.000001"}})
    assert payload["channel"]=="CQA001" and payload["thread_ts"]=="1793300000.000001"
    assert payload["text"]=="슬랙 한국어 답변\n둘째 줄"
    actions=[block for block in payload["blocks"] if block["type"]=="actions"]
    assert [item["text"]["text"] for item in actions[0]["elements"]]==["확인","다음"]
    value=json.loads(actions[0]["elements"][0]["value"])
    assert value["messageId"]==message["id"] and value["sourceTalkNodeId"]=="followup" and value["response"]["buttonValue"]=="확인"


def test_native_identity_dm_bot_and_self_echo_rules():
    first=normalize(event());assert first["participantKind"]=="user" and first["addressed"] is True
    dm=event("개인 질문");dm["event"].update(channel="DQA001",channel_type="im")
    assert normalize(dm)["roomMode"]=="private" and normalize(dm)["addressed"] is True
    bot=event();bot["event"].pop("user");bot["event"].update(type="message",subtype="bot_message",bot_id="BQA002")
    assert normalize(bot)["participantKind"]=="bot"
    bot["event"]["bot_id"]="BQA999";assert normalize(bot) is None
    changed=event();changed["event"]["subtype"]="message_changed";assert normalize(changed) is None
    wrong=event();wrong["team_id"]="TOTHER"
    with pytest.raises(ValueError):normalize(wrong)


@pytest.mark.parametrize("renderer",["form","html","dtmf","form-a-card"])
def test_unrepresentable_slack_template_is_rejected(slack_context,template_context,renderer):
    _,user=template_context
    with pytest.raises(HTTPException) as error:
        admin.create_admin_template(TemplateCreateRequest(channel_code="SLACK",name="지원불가 "+renderer,renderer_type=renderer),_request(),db=slack_context[0][2],current_user=user)
    assert error.value.status_code==400



def stored_message(kind="button", payload=None, text="한국어 답변\n둘째 줄"):
    return {"id":"00000000-0000-4000-8000-000000000001","participantId":"answer-bot","participantKind":"bot","participantName":"답변봇",
        "messageType":kind,"text":text,"createdAt":"2026-10-30T00:00:00+00:00","payload":{"sourceTalkNodeId":"followup",**(payload or {})}}


@pytest.mark.parametrize("kind,payload",[
    ("text",{}),("button",{"options":["확인","다음"]}),
    ("card",{"card":{"title":"카드 제목","description":"설명\n두 줄","imageUrl":"https://example.invalid/card.png"}}),
    ("table",{"columns":["설명","코드"],"rows":[{"설명":"표시값","코드":"B2"}],"keyColumn":"코드","selectable":True}),
    ("link-button",{"options":[{"label":"문서","url":"https://example.invalid/docs"}]}),
    ("carousel",{"carousel":{"title":"묶음 제목","imageUrl":"https://example.invalid/card.png","itemTitle":"항목 제목","itemContents":"항목 설명","buttonType":"Button","itemButtonLabel":"항목 선택","itemButtonValue":"item-2","bottomButtonLabel":"다음","bottomButtonValue":"next"}}),
])
def test_native_payload_preserves_supported_talk_types(kind,payload):
    message=stored_message(kind,payload)
    result=sdk("render",{"message":message,"destination":{"channelId":"CQA001"}})
    assert result["text"]==message["text"] and result["channel"]=="CQA001" and "thread_ts" not in result
    if kind=="text":assert not result.get("blocks")
    else:assert result["blocks"]
    if kind=="card":assert any(block.get("image_url")==payload["card"]["imageUrl"] for block in result["blocks"])
    if kind=="table":
        table=next(block for block in result["blocks"] if block["type"]=="table")
        assert table["rows"][1][1]["text"]=="B2"
        button=next(block for block in result["blocks"] if block["type"]=="actions")["elements"][0]
        assert json.loads(button["value"])["response"]["buttonValue"]=="B2"
    if kind=="link-button":assert next(block for block in result["blocks"] if block["type"]=="actions")["elements"][0]["url"]=="https://example.invalid/docs"
    if kind=="carousel":
        values=[json.loads(item["value"])["response"]["buttonValue"] for block in result["blocks"] if block["type"]=="actions" for item in block["elements"]]
        assert values==["item-2","next"]


def test_native_button_callback_preserves_cga_source():
    message=stored_message("button",{"options":["확인"]})
    rendered=sdk("render",{"message":message,"destination":{"channelId":"CQA001","threadTs":"1793300000.000001"}})
    button=next(block for block in rendered["blocks"] if block["type"]=="actions")["elements"][0]
    raw={"type":"block_actions","team":{"id":"TQA001"},"user":{"id":"UQA001"},"channel":{"id":"CQA001"},"message":{"ts":"1793376000.000900","thread_ts":"1793300000.000001"},
        "container":{"message_ts":"1793376000.000900"},"actions":[{**button,"action_ts":"1793376001.000100"}]}
    value=normalize(raw);wire=json.loads(value["message"])
    assert value["addressed"] is True and value["sourceMessageId"]==message["id"]
    assert wire=={"webchatRichFormVersion":"1.0","sourceTalkNodeId":"followup","response":{"buttonValue":"확인"}}
    assert value["nativeRoomKey"]==normalize(event(thread="1793300000.000001"))["nativeRoomKey"]
    assert value["eventId"]==normalize(raw)["eventId"]


@pytest.mark.parametrize("kind,payload,text",[("html",{},"html"),("form",{"richForm":[]},"form"),("dtmf",{},"dtmf"),("form-a-card",{"adaptiveCard":{}},"card"),("text",{},"x"*40001),("button",{"options":["x"*76]},"버튼")], ids=["html","form","dtmf","adaptive","text-length","button-label-length"])
def test_invalid_native_expression_is_not_silently_dropped(kind,payload,text):
    with pytest.raises(ValueError):sdk("render",{"message":stored_message(kind,payload,text),"destination":{"channelId":"CQA001"}})


@pytest.mark.parametrize("thread",["",0,"not-a-ts"])
def test_invalid_thread_never_falls_back_to_main_room(thread):
    with pytest.raises(ValueError):normalize(event(thread=thread))
    with pytest.raises(ValueError):sdk("render",{"message":stored_message("text"),"destination":{"channelId":"CQA001","threadTs":thread}})


@pytest.mark.parametrize("kind,fields", [
    ("card", ["카드 제목", "", "카드 설명"]),
    ("carousel", ["목록 제목", "", "항목 제목", "항목 내용", "", "", "Hidden Button", "", ""]),
], ids=["card-no-image", "carousel-no-image"])
def test_slack_optional_image_keeps_talk_field_positions(kind, fields):
    from app.api.routes import channels
    node = {"id": "followup", "kind": "talk", "config": {"channelTemplates": {"SLACK": {
        "messageType": kind, "basicMessages": ["본문"], "messages": fields, "responseType": "none"}}}}
    output = channels._talk_output(node, {}, channel_code="SLACK")
    packet = sdk("render", {"message": {"id": "known-card", "messageType": output["type"],
        "text": output["text"], "payload": output["payload"]}, "destination": {"channelId": "CQA001", "threadTs": None}})
    expected = "카드 설명" if kind == "card" else "항목 내용"
    assert expected in json.dumps(packet, ensure_ascii=False)
    assert not any(block["type"] == "image" for block in packet["blocks"])
    assert output["payload"][kind]["imageUrl"] == ""
