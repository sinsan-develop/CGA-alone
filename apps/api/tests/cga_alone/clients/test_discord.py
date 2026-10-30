"""S09 native 표본→기존 질문 API/PG/Talk→Discord packet. 실 플랫폼/Provider 없음."""
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
FIXTURE = json.loads((Path(__file__).parents[1]/"fixtures/client_templates/discord.json").read_text(encoding="utf-8"))


def sdk(operation, value):
    code = r"""
const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(p.root+'/apps/web/node_modules/typescript');
const source=fs.readFileSync(p.root+'/packages/shared/src/channel.ts','utf8'),holder={exports:{}};
new Function('exports','module','process',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(holder.exports,holder,process);
const fn=holder.exports[{normalize:'normalizeAidotDiscordEvent',render:'buildAidotDiscordReplyPayload'}[p.operation]];
if(!fn)console.log(JSON.stringify({missing:p.operation}));else try{const v=p.value;console.log(JSON.stringify({result:p.operation==='normalize'?fn(v.event,v.connection):fn(v.message,v.destination)}));}catch(e){console.log(JSON.stringify({error:e.message}));}
"""
    result=subprocess.run(["node","-e",code],input=json.dumps({"root":str(ROOT),"operation":operation,"value":value},ensure_ascii=False),text=True,capture_output=True,encoding="utf-8")
    assert result.returncode == 0, result.stderr
    data=json.loads(result.stdout);assert "missing" not in data, data
    if "error" in data:raise ValueError(data["error"])
    return data["result"]


def event(text=None, user=None, message_id=None, channel=None):
    item=deepcopy(FIXTURE["addressed"])
    if text is not None:item["d"]["content"]=text
    if user is not None:item["d"]["author"]["id"]=user
    if message_id is not None:item["d"]["id"]=message_id
    if channel is not None:item["d"]["channel_id"]=channel
    return item


def normalized(native, connection=None):return sdk("normalize",{"event":native,"connection":connection or FIXTURE["connection"]})


@pytest.fixture
def discord_context(client_context):
    client_context[0][2].add(AdminChannel(organization_id=client_context[0][0].organization_id,code="DISCORD",name="S09 Discord QA",status="active"))
    client_context[0][2].flush()
    return client_context


def post(context, member, native, connection=None):
    parsed=normalized(native,connection)
    return send(context,member,parsed["message"],"discord",addressed=parsed["addressed"],event=parsed["eventId"])


def test_addressed_message_invokes_bot(discord_context):
    c,_=discord_context;member=create(discord_context,"discord")
    result=post(discord_context,member,event());assert result.status_code==200
    assert result.json()["data"]["botMessage"]["text"]=="추가 설명"
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==1
    assert post(discord_context,member,event("<@100000000000000998> 추가 설명",message_id="100000000000000103")).status_code==200
    assert len(c[3])==1


def test_general_message_does_not_invoke_bot(discord_context):
    c,_=discord_context;member=create(discord_context,"discord")
    result=post(discord_context,member,FIXTURE["general"]);assert result.status_code==200 and result.json()["data"]["botMessages"]==[]
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==UUID(member["roomId"])))==0


def test_other_actor_cannot_continue(discord_context):
    c,client=discord_context;first=create(discord_context,"discord")
    assert post(discord_context,first,event()).status_code==200
    joined=client.post(f"/api/v1/channels/discord/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":"B"});assert joined.status_code==200
    second={**joined.json()["data"],"room":first["room"]};room=c[2].get(ChannelRoom,UUID(first["roomId"]));before=deepcopy(room.metadata_json.get("variables"))
    assert post(discord_context,second,event(user="100000000000000011",message_id="100000000000000104")).status_code==409
    c[2].expire_all();assert room.metadata_json.get("variables")==before and not c[3]
    assert post(discord_context,second,FIXTURE["general"]).status_code==200


def test_duplicate_event_runs_once(discord_context):
    c,_=discord_context;member=create(discord_context,"discord")
    first=post(discord_context,member,event());assert first.status_code==200
    repeated=post(discord_context,member,event());assert repeated.status_code==200
    assert repeated.json()["data"]["userMessage"]["id"]==first.json()["data"]["userMessage"]["id"]
    answer=event("<@100000000000000998> 추가 설명",message_id="100000000000000103")
    completed=post(discord_context,member,answer);assert completed.status_code==200
    assert post(discord_context,member,answer).json()["data"]["botMessage"]["id"]==completed.json()["data"]["botMessage"]["id"]
    assert len(c[3])==1
    assert post(discord_context,member,event("바뀐 내용",message_id="100000000000000103")).status_code==409


def test_same_actor_in_different_rooms_is_isolated(discord_context):
    c,client=discord_context;first=create(discord_context,"discord");second=create(discord_context,"discord")
    other=event(channel="100000000000000003");connection={**FIXTURE["connection"],"channelId":"100000000000000003","channelType":11}
    assert normalized(event())["nativeRoomKey"]!=normalized(other,connection)["nativeRoomKey"]
    assert post(discord_context,first,event()).status_code==200 and post(discord_context,second,other,connection).status_code==200
    assert post(discord_context,first,event("<@100000000000000998> 추가 설명",message_id="100000000000000103")).status_code==200
    result=client.get(f"/api/v1/channels/discord/rooms/{second['roomId']}",headers=member_headers(second));assert result.status_code==200
    assert result.json()["data"]["runtime"]["waitingNodeId"]=="followup" and len(c[3])==1


def test_template_round_trip_to_client_payload(discord_context,template_context):
    c,_=discord_context;_,user=template_context;db=c[2]
    template=admin.create_admin_template(TemplateCreateRequest(channel_code="DISCORD",name="S09 Discord 버튼",renderer_type="button",item_types="button"),_request(),db=db,current_user=user)["data"]
    assert any(str(item["id"])==template["id"] for item in bots.list_talk_templates(_request(),channel_code="DISCORD",db=db,current_user=user)["data"]["items"])
    document=deepcopy(c[1].version_json);node=next(item for item in document["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")
    node["config"]["channelTemplates"]={"DISCORD":FIXTURE["talk"]}
    bots.update_version(c[0].id,c[1].id,VersionUpdateRequest(version_json=document),_request(),current_user=user,db=db)
    db.expire_all();saved=db.get(BotVersion,c[1].id)
    assert next(item for item in saved.version_json["dialog_flow_graphs"][0]["nodes"] if item["id"]=="followup")["config"]["channelTemplates"]["DISCORD"]==FIXTURE["talk"]
    member=create(discord_context,"discord");result=post(discord_context,member,event());assert result.status_code==200
    packet=sdk("render",{"message":result.json()["data"]["botMessage"],"destination":{"channelId":FIXTURE["connection"]["channelId"]}})
    assert packet["channel_id"]==FIXTURE["connection"]["channelId"] and packet["content"]=="디스코드 한국어 답변\n둘째 줄"
    assert packet["allowed_mentions"]=={"parse":[],"replied_user":False}
    assert packet["components"][0]["components"][0]["type"]==2


def test_discord_dm_group_and_bot_identity():
    dm=event("직접 질문");dm["d"].pop("guild_id")
    connection={**FIXTURE["connection"],"guildId":None,"channelType":1}
    assert normalized(dm,connection)["roomMode"]=="private" and normalized(dm,connection)["addressed"]
    group=normalized(dm,{**connection,"channelType":3});assert group["roomMode"]=="shared" and not group["addressed"]
    other=event();other["d"]["author"]["bot"]=True
    assert normalized(other)["participantKind"]=="bot"
    own=event(user=FIXTURE["connection"]["botUserId"]);assert normalized(own) is None
    changed=event();changed["t"]="MESSAGE_UPDATE";assert normalized(changed) is None
    deleted=event();deleted["t"]="MESSAGE_DELETE";assert normalized(deleted) is None


@pytest.mark.parametrize("patch", [{"guildId":"100000000000000099"},{"channelId":"100000000000000099"},{"channelType":999},{"channelId":100000000000000002}],ids=["guild","channel","kind","numeric-id"])
def test_discord_rejects_wrong_native_scope(patch):
    with pytest.raises(ValueError):normalized(event(),{**FIXTURE["connection"],**patch})


@pytest.mark.parametrize("kind",["text","button","card","table","link-button","carousel"])
def test_discord_real_talk_to_native_expressions(kind):
    from app.api.routes import channels
    config={"messageType":kind,"basicMessages":["한국어 본문\n둘째 줄"],"responseType":"single-select"}
    values={}
    if kind=="button":config["messages"]=["선택"]
    elif kind=="card":config["messages"]=["카드 제목","","카드 설명"]
    elif kind=="link-button":config["linkButtonItems"]=[{"label":"문서","url":"https://example.com/한국어"}]
    elif kind=="table":
        config.update(tableVariableItemId="rows",tableColumnMappings=[{"column":"name"},{"column":"id"}],tableKeyColumn="id")
        values={"__items":{"rows":[{"name":"표시 이름","id":"선택 키"}]}}
    elif kind=="carousel":config["messages"]=["목록 제목","","항목 제목","항목 내용","선택","값","Button","하단","하단 값"]
    output=channels._talk_output({"id":"followup","config":{"channelTemplates":{"DISCORD":config}}},values,channel_code="DISCORD")
    packet=sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001","messageType":output["type"],"text":output["text"],"payload":{**output["payload"],"options":output["options"]}},"destination":{"channelId":FIXTURE["connection"]["channelId"]}})
    assert packet["content"]=="한국어 본문\n둘째 줄" and packet["allowed_mentions"]=={"parse":[],"replied_user":False}
    if kind in {"card","carousel"}:assert "image" not in packet["embeds"][0]
    if kind=="card":assert packet["embeds"][0]["description"]=="카드 설명"
    if kind=="table":assert "선택 키" in json.dumps(packet,ensure_ascii=False)
    if kind in {"button","table","carousel"}:
        button=packet["components"][0]["components"][0]
        assert button["custom_id"].startswith("cga:00000000-0000-4000-8000-000000000001:") and len(button["custom_id"])<=100
    if kind=="link-button":assert packet["components"][0]["components"][0]["style"]==5


@pytest.mark.parametrize("kind",["html","form","form-a-card","dtmf"])
def test_discord_unsupported_template_is_explicit(kind):
    assert admin._template_renderer_issues(channel_code="DISCORD",renderer_type=kind)
    with pytest.raises(ValueError):sdk("render",{"message":{"id":"known","messageType":kind,"text":"본문","payload":{}},"destination":{"channelId":FIXTURE["connection"]["channelId"]}})


@pytest.mark.parametrize("message",[
 {"messageType":"text","text":"x"*2001},
 {"messageType":"button","text":"선택","payload":{"options":["x"*81]}},
 {"messageType":"button","text":"선택","payload":{"options":["선택"]*26}},
 {"messageType":"card","text":"카드","payload":{"card":{"title":"카드","imageUrl":"javascript:alert(1)"}}},
],ids=["content","label","choices","url"])
def test_discord_native_limits_are_explicit(message):
    with pytest.raises(ValueError):sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001",**message},"destination":{"channelId":FIXTURE["connection"]["channelId"]}})


def test_discord_callback_source_identity_and_replay(discord_context):
    c,_=discord_context;member=create(discord_context,"discord")
    first=post(discord_context,member,event()).json()["data"]["botMessage"]
    wire=json.dumps({"webchatRichFormVersion":"1.0","sourceMessageId":first["id"],"sourceTalkNodeId":"followup","response":{"buttonValue":"추가 설명"}},ensure_ascii=False)
    answer=send(discord_context,member,wire,"discord",addressed=True,event="discord-interaction:100000000000000201");assert answer.status_code==200
    repeated=send(discord_context,member,wire,"discord",addressed=True,event="discord-interaction:100000000000000201")
    assert repeated.status_code==200 and repeated.json()["data"]["botMessage"]["id"]==answer.json()["data"]["botMessage"]["id"] and len(c[3])==1
    stale=send(discord_context,member,wire,"discord",addressed=True,event="discord-interaction:100000000000000202");assert stale.status_code==409 and len(c[3])==1


@pytest.mark.parametrize("kind",["card","carousel"])
def test_discord_basic_text_with_empty_optional_card_fields(kind):
    from app.api.routes import channels
    output=channels._talk_output({"id":"followup","config":{"channelTemplates":{"DISCORD":{"messageType":kind,"basicMessages":["본문만 표시"],"messages":[],"responseType":"none"}}}}, {},channel_code="DISCORD")
    packet=sdk("render",{"message":{"id":"00000000-0000-4000-8000-000000000001","messageType":output["type"],"text":output["text"],"payload":output["payload"]},"destination":{"channelId":FIXTURE["connection"]["channelId"]}})
    assert packet["content"]=="본문만 표시" and not packet.get("embeds")


def test_discord_health_retains_existing_provider_descriptor():
    from app.api.routes import channels
    details=channels._channel_health_summary()
    discord=next(item for item in details if item["channel"]=="discord")
    assert discord["provider"]=="discord" and discord["renderer"]=="shared-sdk"
    assert discord["supported_outputs"]==["content","embeds","components"]
    assert next(item for item in details if item.get("provider")=="kakao")["supported_outputs"]==["simpleText","quickReplies","basicCard","listCard","carousel"]
