from types import SimpleNamespace
import pytest
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database,event
from app.services import channel_connection_state as state
from app.services.channel_connection_supervisor import ConnectionSupervisor


def accept(c,ev):
    cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        item=state.accept_delivery(db,cid,lease,c.owner,fence,ev);identifier=item.id;db.commit();return identifier


def test_execution_gate_leaves_pending_delivery_unclaimed(native_context):
    from app.models import ChannelConnectionDelivery
    c=native_context;did=accept(c,event());cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        assert state.claim_delivery(db,cid,lease,c.owner,fence,deadline_seconds=30,can_execute=lambda item:False) is None
        item=db.get(ChannelConnectionDelivery,did);assert item.status=='received' and item.execution_claim_id is None;db.commit()
        assert state.claim_delivery(db,cid,lease,c.owner,fence,deadline_seconds=30).id==did


def test_pending_callback_does_not_block_a_different_room(native_context):
    c=native_context;first=accept(c,event());second=accept(c,{**event(),'eventId':'other-room','nativeRoomKey':'independent-room'})
    cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        item=state.claim_delivery(db,cid,lease,c.owner,fence,deadline_seconds=30,can_execute=lambda item:item.id!=first)
        assert item.id==second


def test_supervisor_respects_receiver_execution_gate(native_context):
    from app.models import ChannelConnection
    c=native_context;accept(c,event());cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:row=db.get(ChannelConnection,cid);generation=row.generation
    target=SimpleNamespace(spec=SimpleNamespace(id=cid,generation=generation))
    run=SimpleNamespace(lease_id=lease,fence=fence,listener=SimpleNamespace(can_execute=lambda item:False))
    worker=ConnectionSupervisor(c.sessions,{},owner=c.owner)
    assert worker._claim_work(run,target) is None



def gateway_state(c):
    from importlib import import_module
    lease,fence=c.leases[0]
    return import_module('app.services.channel_connection_discord_gateway').DiscordState(c.sessions,lease_id=lease,owner=c.owner,fence=fence)


def pending(c,*,expired=False,event_id=None):
    from app.core.config import settings
    from app.models import ChannelConnectionDelivery
    from app.services.channel_connections import encrypt_connection_state
    did=accept(c,{**event(),'eventId':event_id or event()['eventId']})
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did)
        ctx={'discord_ack':{'status':'pending','expires_at':state.clock(db).timestamp()+(-1 if expired else 30)},'interaction_id':'600','callback_token':'synthetic-callback-token'}
        item.context_ciphertext=encrypt_connection_state(ctx,item.organization_id,item.connection_id,settings.channel_connection_encryption_key,'v1','delivery:'+str(did));item.context_key_version='v1';db.commit()
    return did


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_callback_is_reserved_once_and_confirmed_before_execution(native_context):
    from app.models import ChannelConnectionDelivery
    c=native_context;did=pending(c);gate=gateway_state(c);cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:assert not gate.can_execute(db.get(ChannelConnectionDelivery,did))
    reservation=gate.reserve_ack(did);assert reservation.interaction_id=='600' and 'synthetic-callback-token' not in repr(reservation)
    assert gate.reserve_ack(did) is None
    with c.sessions() as db:assert state.claim_delivery(db,cid,lease,c.owner,fence,deadline_seconds=30,can_execute=gate.can_execute) is None
    assert gate.finish_ack(did,reservation.claim_id,'confirmed')
    with c.sessions() as db:
        item=state.claim_delivery(db,cid,lease,c.owner,fence,deadline_seconds=30,can_execute=gate.can_execute);assert item.id==did and item.status=='executing'
        assert 'callback_token' not in gate.context(item)


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_unknown_callback_never_retries_or_executes(native_context):
    from app.models import ChannelConnectionDelivery
    c=native_context;did=pending(c);gate=gateway_state(c);reservation=gate.reserve_ack(did)
    assert gate.finish_ack(did,reservation.claim_id,'unknown') and gate.reserve_ack(did) is None
    assert not gate.finish_ack(did,reservation.claim_id,'confirmed')
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);assert item.status=='unknown' and not gate.can_execute(item) and 'callback_token' not in gate.context(item)


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_expired_unsubmitted_callback_and_changed_generation_cannot_execute(native_context):
    from app.models import ChannelConnection,ChannelConnectionDelivery
    c=native_context;did=pending(c,expired=True);gate=gateway_state(c);assert gate.reserve_ack(did) is None
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);assert item.status=='failed' and 'callback_token' not in gate.context(item)
    other=pending(c,event_id='generation-changed')
    with c.sessions() as db:db.get(ChannelConnection,c.ids['connections'][0]).generation+=1;db.commit()
    assert gate.reserve_ack(other) is None
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,other);assert item.status=='cancelled' and 'callback_token' not in gate.context(item)


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_native_cursor_is_protected_and_only_current_lease_can_advance(native_context):
    from dataclasses import replace
    from fastapi import HTTPException
    from app.models import ChannelInstallationLease
    from importlib import import_module
    c=native_context;gate=gateway_state(c);value={'application_id':'100','bot_user_id':'200','token_hash':'a'*64,'session_id':'synthetic-session','resume_url':'wss://gateway.discord.gg/?v=10&encoding=json','sequence':7}
    gate.save_cursor(value);assert gate.load_cursor()==value
    with c.sessions() as db:assert 'synthetic-session' not in str(db.get(ChannelInstallationLease,c.leases[0][0]).cursor_json)
    other=import_module('app.services.channel_connection_discord_gateway').DiscordState(c.sessions,lease_id=c.leases[0][0],owner='other-owner',fence=c.leases[0][1])
    with pytest.raises(HTTPException):other.save_cursor({**value,'sequence':8})
    assert gate.load_cursor()['sequence']==7

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_cursor_rejects_credentials_and_invalid_resume_destination(native_context):
    from fastapi import HTTPException
    gate=gateway_state(native_context)
    for value in [{'bot_token':'must-not-store'},{'application_id':'100','bot_user_id':'200','token_hash':'a'*64,'session_id':'s','resume_url':'wss://evil.test','sequence':1}]:
        with pytest.raises(HTTPException):gate.save_cursor(value)
    assert gate.load_cursor() is None

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_busy_rejected_button_removes_callback_token_without_submitting(native_context):
    from app.models import ChannelConnectionDelivery
    c=native_context;did=pending(c);gate=gateway_state(c)
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);item.status='failed';item.last_error_code='ROOM_BUSY';db.commit()
    assert gate.reserve_ack(did) is None
    with c.sessions() as db:assert 'callback_token' not in gate.context(db.get(ChannelConnectionDelivery,did))

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_callback_inflight_crash_becomes_unknown_without_reclaim(native_context):
    from app.models import ChannelConnectionDelivery
    c=native_context;did=pending(c);gate=gateway_state(c);reservation=gate.reserve_ack(did)
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);ctx=gate.context(item);ctx['discord_ack']['expires_at']=0;gate._write(item,ctx);db.commit()
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);assert not gate.can_execute(item);db.commit()
    assert gate.reserve_ack(did) is None and not gate.finish_ack(did,reservation.claim_id,'confirmed')
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);assert item.status=='unknown' and 'callback_token' not in gate.context(item)

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_gateway_reset_preserves_unknown_delivery_and_records_unreceived_range(native_context):
    from app.models import AuditLog,ChannelConnectionDelivery,ChannelInstallationLease
    from sqlalchemy import select,delete
    c=native_context;gate=gateway_state(c);did=accept(c,event());lid=c.leases[0][0]
    with c.sessions() as db:item=db.get(ChannelConnectionDelivery,did);item.status='unknown';db.commit()
    try:
        gate.reset_cursor([], 'DISCORD_RESUME_WINDOW_EXCEEDED',7,72)
        assert not gate.load_cursor()
        with c.sessions() as db:
            assert db.get(ChannelConnectionDelivery,did).status=='unknown'
            audit=db.scalar(select(AuditLog).where(AuditLog.target_id==lid));assert audit.after_json['from_sequence']==7 and audit.after_json['through_sequence']==72 and audit.after_json['end_unknown'] is True
            assert 'discord_gateway_reset' in db.get(ChannelInstallationLease,lid).cursor_json
    finally:
        with c.sessions() as db:db.execute(delete(AuditLog).where(AuditLog.target_id==lid));db.commit()
