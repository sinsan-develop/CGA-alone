"""S05 독립 차례 핵심. 실제 PG 두 연결을 사용하며 API/인증 소비 검증은 별도다."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib import import_module,util
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
import json
from pathlib import Path
import pytest
from sqlalchemy import select,delete,update,func
from sqlalchemy.orm import Session
from app.models import Organization,Group,User,Bot,BotVersion,ChannelRoom,ChannelMessage,ChannelQueueEvent
from app.core.version_documents import build_default_version_document


def subject():
    assert util.find_spec("app.services.room_turn") is not None,"S05 DB 원자적 차례 helper가 필요합니다"
    return import_module("app.services.room_turn")


@pytest.fixture
def turn_context(test_engine,migrated_test_database):
    assert test_engine.dialect.name=="postgresql"
    with Session(test_engine) as db:
        org=Organization(code="s05-turn-"+uuid4().hex,name="S05 차례 검증")
        db.add(org);db.flush()
        group=Group(organization_id=org.id,code="s05-"+uuid4().hex,name="S05 검증그룹")
        db.add(group);db.flush()
        user=User(organization_id=org.id,group_id=group.id,login_id="s05-"+uuid4().hex[:20],name="S05 합성 제작자",password_hash="test-only",status="active")
        db.add(user);db.flush()
        bot=Bot(organization_id=org.id,group_id=group.id,created_by=user.id,name="S05 차례 검증봇",data_json={})
        db.add(bot);db.flush()
        version=BotVersion(bot_id=bot.id,version_no=1,name="v1",created_by=user.id,version_json=build_default_version_document())
        db.add(version);db.flush()
        rooms=[ChannelRoom(channel_type="webchat",client_room_id="s05-"+uuid4().hex,bot_id=bot.id,bot_version_id=version.id,participant_id="server:A",metadata_json={"variables":{"kept":"기존 변수"}}) for _ in range(2)]
        db.add_all(rooms);db.flush();db.commit()
        c=SimpleNamespace(engine=test_engine,org_id=org.id,group_id=group.id,user_id=user.id,bot_id=bot.id,version_id=version.id,room_ids=[r.id for r in rooms])
    directory=Path(__file__).resolve().parents[4]/".local/s05-fixture-owners";directory.mkdir(parents=True,exist_ok=True)
    receipt=directory/(str(c.org_id)+".json");receipt.write_text(json.dumps({k:str(getattr(c,k)) for k in ["org_id","group_id","user_id","bot_id","version_id"]}),encoding="utf-8")
    try:yield c
    finally:
        with Session(test_engine) as db:
            owned=select(ChannelRoom.id).where(ChannelRoom.bot_id==c.bot_id)
            db.execute(delete(ChannelQueueEvent).where(ChannelQueueEvent.bot_id==c.bot_id))
            db.execute(delete(ChannelMessage).where(ChannelMessage.room_id.in_(owned)))
            db.execute(delete(ChannelRoom).where(ChannelRoom.bot_id==c.bot_id))
            db.execute(update(Bot).where(Bot.id==c.bot_id).values(active_version_id=None))
            db.execute(delete(BotVersion).where(BotVersion.bot_id==c.bot_id))
            db.execute(delete(Bot).where(Bot.id==c.bot_id));db.execute(delete(User).where(User.id==c.user_id))
            db.execute(delete(Group).where(Group.id==c.group_id));db.execute(delete(Organization).where(Organization.id==c.org_id));db.commit()
        receipt.unlink()


def claim(c,actor="server:A",index=0,addressed=True):
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[index]);result=subject().claim_room_turn(db,room,actor,addressed);db.commit();return result


def test_parallel_claim_allows_one_actor(turn_context):
    c=turn_context;barrier=Barrier(2)
    def run(actor):barrier.wait(timeout=5);return claim(c,actor)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,["server:A","server:B"]))
    assert sum(r.allowed for r in results)==1
    assert len([r.generation for r in results if r.generation])==1
    assert all(r.reason=="another_actor" for r in results if not r.allowed)


def test_same_actor_inflight_request_is_serialized(turn_context):
    first=claim(turn_context);second=claim(turn_context)
    assert first.allowed and not second.allowed and second.reason=="processing" and second.generation is None


def test_waiting_talk_retains_turn_and_owner_gets_new_generation(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation);db.commit()
    assert not claim(c,"server:B").allowed
    followup=claim(c);assert followup.allowed and followup.generation!=first.generation
    with Session(c.engine) as db:
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,"dialog_ended")
        assert not s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation)
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",followup.generation) is not None
        db.commit()


def test_general_chat_does_not_claim_or_change_runtime(turn_context):
    c=turn_context;result=claim(c,addressed=False)
    assert not result.allowed and result.reason=="not_addressed"
    with Session(c.engine) as db:assert db.get(ChannelRoom,c.room_ids[0]).metadata_json=={"variables":{"kept":"기존 변수"}}


@pytest.mark.parametrize("actor",["","  "])
def test_missing_trusted_actor_is_rejected(turn_context,actor):
    result=claim(turn_context,actor);assert not result.allowed and result.reason=="unbound_actor"


def test_different_rooms_can_run_same_bot_without_waiting_for_first_lock(turn_context):
    c=turn_context;s=subject();pool=ThreadPoolExecutor(max_workers=1)
    with Session(c.engine) as first_db:
        room=first_db.get(ChannelRoom,c.room_ids[0]);assert s.claim_room_turn(first_db,room,"server:A",True).allowed
        future=pool.submit(claim,c,"server:B",1)
        try:assert future.result(timeout=5).allowed
        finally:first_db.rollback();pool.shutdown(wait=True)


def test_stale_completion_cannot_release_or_write_new_turn(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:assert s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,"dialog_ended");db.commit()
    newer=claim(c,"server:B");assert newer.allowed
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);before=deepcopy(room.metadata_json)
        stale=s.lock_current_room_turn(db,room.id,"server:A",first.generation)
        if stale is not None:db.add(ChannelMessage(room_id=room.id,channel_type="webchat",participant_id="bot",participant_kind="bot",text="stale answer"))
        assert stale is None and not s.release_room_turn(db,room.id,"server:A",first.generation,"error")
        assert room.metadata_json==before;db.commit()
    with Session(c.engine) as db:assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==c.room_ids[0]))==0


@pytest.mark.parametrize("reason",["dialog_ended","session_ended","error","cancelled"])
def test_failure_and_session_end_release_only_owned_generation(turn_context,reason):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert not s.release_room_turn(db,c.room_ids[0],"server:B",first.generation,reason)
        assert s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,reason)
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,reason);db.commit()
    assert claim(c,"server:B").allowed
    with Session(c.engine) as db:assert db.get(ChannelRoom,c.room_ids[0]).metadata_json["variables"]=={"kept":"기존 변수"}


def test_waiting_reason_is_not_a_terminal_release(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,"waiting_user_input");db.commit()
    assert not claim(c,"server:B").allowed


@pytest.mark.parametrize("closed",[True,False])
def test_closed_or_deleted_room_cannot_be_claimed(turn_context,closed):
    c=turn_context
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0])
        if closed:room.status="closed"
        else:
            from datetime import datetime,timezone
            room.deleted_at=datetime.now(timezone.utc)
        db.commit()
    result=claim(c);assert not result.allowed and result.reason=="room_closed"


def test_corrupt_turn_is_not_silently_replaced(turn_context):
    c=turn_context
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);room.metadata_json={**room.metadata_json,"__roomTurn":{"actor_id":"server:A"}};db.commit()
    result=claim(c);assert not result.allowed and result.reason=="invalid_turn"


def test_stale_version_snapshot_is_not_claimed(turn_context):
    c=turn_context;s=subject()
    with Session(c.engine) as db:
        stale=SimpleNamespace(id=c.room_ids[0],bot_id=c.bot_id,bot_version_id=uuid4())
        result=s.claim_room_turn(db,stale,"server:A",True);db.commit()
    assert not result.allowed and result.reason=="version_changed"


def test_claim_does_not_commit_callers_transaction(turn_context):
    c=turn_context;s=subject()
    with Session(c.engine) as first:
        room=first.get(ChannelRoom,c.room_ids[0]);assert s.claim_room_turn(first,room,"server:A",True).allowed
        with Session(c.engine) as observer:assert "__roomTurn" not in observer.get(ChannelRoom,room.id).metadata_json
        first.rollback()
    assert claim(c,"server:B").allowed


def test_duplicate_settlement_does_not_reopen_or_write(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation)
        assert not s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation)
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",first.generation) is None
        db.commit()


def test_expired_waiting_turn_can_start_new_generation_without_old_completion(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation)
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["waiting_expires_at"]="2000-01-01T00:00:00+00:00";room.metadata_json=metadata;db.commit()
    newer=claim(c,"server:B");assert newer.allowed and newer.reason=="expired" and newer.generation!=first.generation
    with Session(c.engine) as db:
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",first.generation) is None
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,"error");db.commit()


@pytest.mark.parametrize("scope_key",["id","name"])
def test_waiting_deadline_uses_existing_version_timeout(turn_context,scope_key):
    from datetime import datetime
    c=turn_context;s=subject()
    with Session(c.engine) as db:
        bot=db.get(Bot,c.bot_id);key=str(c.version_id) if scope_key=="id" else "v1"
        bot.data_json={"settings_by_version":{key:{"conversationDefaults":{"timeout":{"enabled":True,"seconds":10}}}}};db.commit()
    first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation);db.commit()
        expires=datetime.fromisoformat(db.get(ChannelRoom,c.room_ids[0]).metadata_json["__roomTurn"]["waiting_expires_at"])
        now=db.scalar(select(func.clock_timestamp()));assert 0<(expires-now).total_seconds()<=10
    assert not claim(c,"server:B").allowed


def test_disabled_waiting_timeout_is_preserved(turn_context):
    c=turn_context;s=subject()
    with Session(c.engine) as db:
        bot=db.get(Bot,c.bot_id);bot.data_json={"settings_by_version":{"v1":{"conversationDefaults":{"timeout":{"enabled":False,"seconds":1}}}}};db.commit()
    first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation);db.commit()
        assert db.get(ChannelRoom,c.room_ids[0]).metadata_json["__roomTurn"]["waiting_expires_at"] is None
    assert not claim(c,"server:B").allowed


def test_corrupt_expiry_does_not_allow_another_actor_to_take_over(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert s.hold_room_turn_for_input(db,c.room_ids[0],"server:A",first.generation)
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["waiting_expires_at"]="invalid";room.metadata_json=metadata;db.commit()
    decision=claim(c,"server:B");assert not decision.allowed and decision.reason=="invalid_turn"


def test_wrong_bot_marker_is_not_silently_rebound(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["bot_id"]=str(uuid4());room.metadata_json=metadata;db.commit()
    decision=claim(c);assert not decision.allowed and decision.reason=="invalid_turn"


@pytest.mark.parametrize("addressed",["false","true",1,None])
def test_only_boolean_addressed_can_claim(turn_context,addressed):
    result=claim(turn_context,addressed=addressed)
    assert not result.allowed and result.reason=="not_addressed"


def test_untyped_terminal_reason_cannot_release(turn_context):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,{"reason":"error"});db.commit()
    assert not claim(c,"server:B").allowed


def test_processing_lease_expiry_fences_old_result_without_replaying(turn_context):
    c=turn_context;s=subject();first=claim(c)
    assert callable(getattr(s,"renew_room_turn_processing_lease",None)),"S05 처리중 복구 lease가 필요합니다"
    with Session(c.engine) as db:
        assert s.renew_room_turn_processing_lease(db,c.room_ids[0],"server:A",first.generation,lease_seconds=30)
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["processing_expires_at"]="2000-01-01T00:00:00+00:00";room.metadata_json=metadata;db.commit()
    with Session(c.engine) as db:
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",first.generation) is None
        assert not s.renew_room_turn_processing_lease(db,c.room_ids[0],"server:A",first.generation,lease_seconds=30);db.commit()
    newer=claim(c,"server:B");assert newer.allowed and newer.reason=="expired" and newer.generation!=first.generation
    with Session(c.engine) as db:
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first.generation,"error")
        assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==c.room_ids[0]))==0;db.commit()


def test_processing_lease_can_only_be_renewed_by_current_owner(turn_context):
    from datetime import datetime
    c=turn_context;s=subject();first=claim(c)
    assert callable(getattr(s,"renew_room_turn_processing_lease",None))
    with Session(c.engine) as db:
        assert not s.renew_room_turn_processing_lease(db,c.room_ids[0],"server:B",first.generation,lease_seconds=30)
        assert s.renew_room_turn_processing_lease(db,c.room_ids[0],"server:A",first.generation,lease_seconds=30);db.commit()
        room=db.get(ChannelRoom,c.room_ids[0]);expires=datetime.fromisoformat(room.metadata_json["__roomTurn"]["processing_expires_at"])
        now=db.scalar(select(func.clock_timestamp()));assert 0<(expires-now).total_seconds()<=30
        assert s.hold_room_turn_for_input(db,room.id,"server:A",first.generation)
        assert not s.renew_room_turn_processing_lease(db,room.id,"server:A",first.generation,lease_seconds=30);db.commit()


@pytest.mark.parametrize("seconds",[0,-1,"30",True,float("inf"),float("nan")])
def test_invalid_processing_lease_does_not_change_owner(turn_context,seconds):
    c=turn_context;s=subject();first=claim(c)
    assert callable(getattr(s,"renew_room_turn_processing_lease",None))
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);before=deepcopy(room.metadata_json)
        assert not s.renew_room_turn_processing_lease(db,room.id,"server:A",first.generation,lease_seconds=seconds)
        assert room.metadata_json==before;db.commit()


@pytest.mark.parametrize("expires",["invalid","2000-01-01T00:00:00",42])
def test_corrupt_processing_deadline_fails_closed(turn_context,expires):
    c=turn_context;s=subject();first=claim(c)
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["processing_expires_at"]=expires;room.metadata_json=metadata;db.commit()
    denied=claim(c,"server:B");assert not denied.allowed and denied.reason=="invalid_turn"
    with Session(c.engine) as db:
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",first.generation) is None
        assert not s.renew_room_turn_processing_lease(db,c.room_ids[0],"server:A",first.generation,lease_seconds=30);db.commit()


def test_processing_owner_process_exit_preserves_lock_and_expiry_fences_old_result(turn_context):
    import os,subprocess,sys
    c=turn_context;s=subject()
    script="""
import json,os,sys
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.models import ChannelRoom
from app.services.room_turn import claim_room_turn,renew_room_turn_processing_lease
payload=json.load(sys.stdin)
url=os.environ['AIDOT_TEST_DATABASE_URL']
assert make_url(url).database=='cga_alone_qa'
engine=create_engine(url)
with Session(engine) as db:
    room=db.get(ChannelRoom,UUID(payload['room']))
    result=claim_room_turn(db,room,payload['actor'],True)
    if result.allowed:
        assert renew_room_turn_processing_lease(db,room.id,payload['actor'],result.generation,lease_seconds=60)
    db.commit()
print(json.dumps({'allowed':result.allowed,'generation':result.generation,'reason':result.reason}),flush=True)
os._exit(0)
"""
    def run(actor):
        result=subprocess.run([sys.executable,"-c",script],input=json.dumps({"room":str(c.room_ids[0]),"actor":actor}),text=True,capture_output=True,timeout=20,env=os.environ.copy())
        assert result.returncode==0,"소유 QA 방 하위 프로세스 검증 실패"
        return json.loads(result.stdout)
    first=run("server:A");assert first["allowed"]
    denied=run("server:B");assert not denied["allowed"] and denied["reason"]=="another_actor"
    with Session(c.engine) as db:
        room=db.get(ChannelRoom,c.room_ids[0]);metadata=deepcopy(room.metadata_json);metadata["__roomTurn"]["processing_expires_at"]="2000-01-01T00:00:00+00:00";room.metadata_json=metadata;db.commit()
    newer=run("server:B");assert newer["allowed"] and newer["reason"]=="expired" and newer["generation"]!=first["generation"]
    with Session(c.engine) as db:
        assert s.lock_current_room_turn(db,c.room_ids[0],"server:A",first["generation"]) is None
        assert not s.release_room_turn(db,c.room_ids[0],"server:A",first["generation"],"error")
        assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id==c.room_ids[0]))==0;db.commit()
