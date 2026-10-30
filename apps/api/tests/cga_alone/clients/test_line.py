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
