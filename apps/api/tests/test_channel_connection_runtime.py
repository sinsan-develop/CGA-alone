from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID,uuid4
import base64,json,sys
import pytest
from fastapi import HTTPException
from sqlalchemy import select,delete,update,func
from sqlalchemy.orm import Session,sessionmaker
from tests.test_channel_connections import test_engine,migrated_test_database
from tests.integration.test_bot_persistence_regression import _request


def subject():return import_module("app.services.channel_connection_runtime")


@pytest.fixture
def native_context(test_engine,migrated_test_database,monkeypatch,tmp_path,request):
    from app.models import Organization,Group,Bot,BotVersion,AdminChannel,ChannelConnection,ChannelConnectionDelivery,ChannelConnectionRoom,ChannelInstallationLease,ChannelRoom,ChannelMessage,ChannelQueueEvent,AuditLog,VersionDialogAsset,VersionDialogFlowGraph
    from app.api.routes import bots,channels,webchat
    from app.core.config import settings
    from app.services.llm_client import LlmChatClient
    from app.services import vector_search
    from app.services.channel_connection_state import claim_installation,activate_connection
    from tests.cga_alone.test_two_stage_runtime import native_secondary_document
    sessions=sessionmaker(bind=test_engine,expire_on_commit=False)
    for module in list(sys.modules.values()):
        if module and getattr(module,"__name__","").startswith("app.") and hasattr(module,"SessionLocal"):monkeypatch.setattr(module,"SessionLocal",sessions)
    monkeypatch.setattr(settings,"channel_connection_encryption_key",base64.urlsafe_b64encode(bytes(range(32))).decode())
    monkeypatch.setattr(settings,"webchat_api_key","c02-synthetic-shared-key")
    monkeypatch.setattr(settings,"nlu_model_storage_path",str(tmp_path/'models'))
    def forbidden(*args,**kwargs):raise AssertionError("C02 QA external/model call forbidden")
    monkeypatch.setattr(LlmChatClient,"chat",forbidden);monkeypatch.setattr(vector_search,"_post_json",forbidden);monkeypatch.setattr(channels,"urlopen",forbidden)
    suffix=uuid4().hex
    provider=getattr(request,"param","slack")
    with sessions() as db:
        org=Organization(code="c02-native-"+suffix,name="C02 native QA");db.add(org);db.flush()
        group=Group(organization_id=org.id,code="C02",name="C02");db.add(group);db.flush()
        bot=Bot(organization_id=org.id,group_id=group.id,name="C02 고정답변",data_json={});db.add(bot);db.flush()
        doc=native_secondary_document.__wrapped__()
        for dialog in doc["dialogs"]:dialog["entityBindings"]=[]
        doc["dialogs"][1]["utterances"]=[{"text":"검증 질문","utteranceType":"T"}]
        version=BotVersion(bot_id=bot.id,version_no=1,name="C02 QA",status="active",version_json=doc);db.add(version);db.flush();bot.active_version_id=version.id
        parent=AdminChannel(organization_id=org.id,code=provider.upper().replace("-","_"),name="C02",data_json={"provider":provider,"renderer_type":provider});db.add(parent);db.flush()
        connections=[ChannelConnection(organization_id=org.id,channel_id=parent.id,bot_id=bot.id,name="C02 "+str(i),provider=provider,installation_id="TQA001",account_id="BQA99"+str(i),target_id="CQA001",desired_state="connected",observed_state="connecting") for i in range(2)]
        db.add_all(connections);db.commit()
        ids=dict(org_id=org.id,group_id=group.id,bot_id=bot.id,version_id=version.id,parent_id=parent.id,connections=[c.id for c in connections]);leases=[]
        for row in connections:
            lease=claim_installation(db,row,"c02-qa-worker",ttl_seconds=60);db.commit()
            activate_connection(db,row.id,lease.id,"c02-qa-worker",lease.fence,{"installation_id":row.installation_id,"account_id":row.account_id});db.commit();leases.append((lease.id,lease.fence))
    receipt=Path(__file__).resolve().parents[3]/".local/channel-connections-c02"/(suffix+'.owner.json');receipt.parent.mkdir(parents=True,exist_ok=True);receipt.write_text(json.dumps(ids,default=str),encoding="utf-8")
    context=SimpleNamespace(sessions=sessions,ids=ids,leases=leases,owner="c02-qa-worker",request=_request())
    try:yield context
    finally:
        with sessions() as db:
            assert db.get(Organization,ids["org_id"]).code=="c02-native-"+suffix
            rooms=list(db.scalars(select(ChannelRoom.id).where(ChannelRoom.bot_id==ids["bot_id"])))
            db.execute(delete(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id.in_(ids["connections"])))
            db.execute(delete(ChannelConnectionRoom).where(ChannelConnectionRoom.connection_id.in_(ids["connections"])))
            db.execute(delete(ChannelConnection).where(ChannelConnection.id.in_(ids["connections"])))
            db.execute(delete(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==ids["org_id"]))
            db.execute(delete(ChannelQueueEvent).where(ChannelQueueEvent.bot_id==ids["bot_id"]))
            db.execute(delete(ChannelMessage).where(ChannelMessage.room_id.in_(rooms)))
            db.execute(delete(ChannelRoom).where(ChannelRoom.id.in_(rooms)))
            db.execute(delete(AuditLog).where(AuditLog.target_id.in_([ids["bot_id"],*rooms])))
            db.execute(delete(VersionDialogAsset).where(VersionDialogAsset.version_id==ids["version_id"]))
            db.execute(delete(VersionDialogFlowGraph).where(VersionDialogFlowGraph.version_id==ids["version_id"]))
            db.execute(update(Bot).where(Bot.id==ids["bot_id"]).values(active_version_id=None))
            db.execute(delete(BotVersion).where(BotVersion.id==ids["version_id"]))
            db.execute(delete(Bot).where(Bot.id==ids["bot_id"]))
            db.execute(delete(AdminChannel).where(AdminChannel.id==ids["parent_id"]))
            db.execute(delete(Group).where(Group.id==ids["group_id"]))
            db.execute(delete(Organization).where(Organization.id==ids["org_id"]));db.commit()
            assert db.get(Organization,ids["org_id"]) is None
        receipt.unlink()


def event(actor="slack:user:UQA001",text="검증 질문"):
    return {"eventId":"c02-native-1","nativeRoomKey":"slack:same-room","nativeParticipantKey":actor,"participantKind":"user","participantName":actor,"roomMode":"shared","addressed":True,"message":text}


def member(context,index=0,ev=None):
    return subject().ensure_connection_membership(context.sessions,context.ids["connections"][index],context.leases[index][0],context.owner,context.leases[index][1],ev or event(),context.request)


def test_member_restarts_reuse_existing_room_and_token_is_encrypted(native_context):
    from app.models import ChannelConnectionRoom
    c=native_context;a=member(c);b=member(c)
    assert a.room_id==b.room_id and a.participant_id==b.participant_id and a.conversation_token==b.conversation_token
    assert a.conversation_token not in repr(a)
    with c.sessions() as db:
        row=db.scalar(select(ChannelConnectionRoom).where(ChannelConnectionRoom.connection_id==c.ids["connections"][0]))
        assert row.room_id==a.room_id and row.participant_ciphertext and a.conversation_token not in row.participant_ciphertext


def test_connections_are_isolated_and_shared_room_actors_have_distinct_proofs(native_context):
    c=native_context;a=member(c);b=member(c,1);other=member(c,ev=event("slack:user:UQA002"))
    assert a.room_id!=b.room_id and a.conversation_token!=b.conversation_token
    assert a.room_id==other.room_id and a.participant_id!=other.participant_id and a.conversation_token!=other.conversation_token


def test_shared_key_and_room_id_cannot_issue_members_of_connected_room(native_context):
    from app.api.routes import webchat,channels
    from app.core.config import settings
    c=native_context;a=member(c)
    with pytest.raises(HTTPException):
        webchat._join_client_room("slack",a.room_id,channels.ChannelParticipantRequest(participant_name="forged"),c.request,settings.webchat_api_key)
    with pytest.raises(HTTPException):channels.get_channel_room("slack",a.room_id,c.request,settings.webchat_api_key)


def test_existing_fixed_qa_runtime_and_duplicate_ledger_are_reused(native_context):
    from app.models import ChannelConnectionDelivery,ChannelMessage
    from app.services import channel_connection_state as state
    c=native_context;connection=c.ids["connections"][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        delivery=state.accept_delivery(db,connection,lease,c.owner,fence,event());db.commit()
        admitted=state.claim_delivery(db,connection,lease,c.owner,fence,deadline_seconds=60);did=admitted.id;claim=admitted.execution_claim_id;db.commit()
    result=subject().execute_connection_delivery(c.sessions,did,lease,c.owner,fence,claim,c.request)
    assert result["botMessages"] and "정의한 로컬 답변" in str(result["botMessages"])
    with c.sessions() as db:
        stored=db.get(ChannelConnectionDelivery,did);assert stored.status=="ready" and stored.room_id and stored.request_message_id and stored.queue_event_id
        assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==stored.room_id))==2
        repeated=state.accept_delivery(db,connection,lease,c.owner,fence,event());db.commit();assert repeated.id==did
        assert state.claim_delivery(db,connection,lease,c.owner,fence,deadline_seconds=60) is None;db.commit()
    with pytest.raises(HTTPException):subject().execute_connection_delivery(c.sessions,did,lease,c.owner,fence,claim,c.request)
    with c.sessions() as db:
        stored=db.get(ChannelConnectionDelivery,did);assert stored.status=="ready"
        assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==stored.room_id))==2


def test_protected_runtime_state_is_bound_to_purpose_and_connection():
    from app.services import channel_connections as security
    key=base64.urlsafe_b64encode(bytes(range(32))).decode();org,connection=uuid4(),uuid4()
    value=security.encrypt_connection_state({"members":{"a":{"token":"synthetic-proof"}}},org,connection,key,"v1","room:"+str(uuid4()))
    assert "synthetic-proof" not in value
    with pytest.raises(HTTPException):security.decrypt_connection_state(value,org,connection,key,"v1","delivery:"+str(uuid4()))


def test_concurrent_native_member_requests_create_one_existing_room(native_context):
    from concurrent.futures import ThreadPoolExecutor
    from app.models import ChannelRoom,ChannelConnectionRoom
    c=native_context
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda _:member(c),range(2)))
    assert values[0].room_id==values[1].room_id and values[0].conversation_token==values[1].conversation_token
    with c.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ChannelRoom).where(ChannelRoom.bot_id==c.ids["bot_id"]))==1
        assert db.scalar(select(func.count()).select_from(ChannelConnectionRoom).where(ChannelConnectionRoom.connection_id==c.ids["connections"][0]))==1


def test_private_native_room_cannot_be_joined_as_another_actor(native_context):
    c=native_context;ev={**event(),"roomMode":"private"}
    a=member(c,ev=ev)
    with pytest.raises(HTTPException):member(c,ev={**ev,"nativeParticipantKey":"slack:user:UQA002"})


@pytest.mark.parametrize("native_context",["discord"],indirect=True)
def test_compact_card_rejects_other_actor_without_blocking_owner_and_restores_variable(native_context):
    from copy import deepcopy
    from app.models import BotVersion,ChannelConnectionDelivery
    from app.services import channel_connection_state as state
    c=native_context;connection=c.ids["connections"][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        version=db.get(BotVersion,c.ids["version_id"]);doc=deepcopy(version.version_json);graph=doc["dialog_flow_graphs"][1]
        talk=next(n for n in graph["nodes"] if n["kind"]=="talk")
        talk["config"]={"messageType":"button","basicMessages":["선택하세요"],"messages":["확인","다음"],"responseType":"relay","responseVariableName":"choice"}
        graph["nodes"].append({"id":"result-talk","kind":"talk","config":{"messageType":"text","basicMessages":["선택: {{$choice}}"],"responseType":"none"}})
        for link in graph["links"]:
            if link["sourceNodeId"]==talk["id"]:link["targetNodeId"]="result-talk"
        graph["links"].append({"sourceNodeId":"result-talk","sourcePort":"next","targetNodeId":"local-end"})
        version.version_json=doc;db.commit()
    def execute(ev):
        with c.sessions() as db:
            delivery=state.accept_delivery(db,connection,lease,c.owner,fence,ev);db.commit()
            claim=state.claim_delivery(db,connection,lease,c.owner,fence,deadline_seconds=60)
            assert claim is not None
            did,token=claim.id,claim.execution_claim_id;db.commit()
        return did,subject().execute_connection_delivery(c.sessions,did,lease,c.owner,fence,token,c.request)
    first_event={**event(),"channelId":"100000000000000002"}
    did,first=execute(first_event);source=first["botMessages"][0]
    assert source["messageType"]=="button" and source["payload"]["sourceTalkNodeId"]
    with c.sessions() as db:
        send=state.claim_send(db,did,lease,c.owner,fence);db.commit()
        state.record_sent(db,did,send,["synthetic-card-ack"]);db.commit()
    selected={**first_event,"eventId":"selected","message":"compact-selection","sourceMessageId":source["id"],"choiceIndex":1}
    with pytest.raises(HTTPException):execute({**selected,"eventId":"wrong-actor","nativeParticipantKey":"discord:user:another"})
    with c.sessions() as db:
        wrong=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.event_key==state.scoped_hash("wrong-actor"),ChannelConnectionDelivery.connection_id==connection))
        assert wrong.status=="failed"
    _,result=execute(selected)
    assert "다음" in str(result["botMessages"]) and "compact-selection" not in str(result["botMessages"])
