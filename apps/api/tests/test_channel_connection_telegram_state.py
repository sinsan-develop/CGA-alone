import pytest
from importlib import import_module
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database,event
from tests.test_channel_connection_discord_state import accept
from app.services import channel_connection_state as state


def gate(c):
    lid,fence=c.leases[0]
    return import_module('app.services.channel_connection_telegram_polling').TelegramState(c.sessions,lease_id=lid,owner=c.owner,fence=fence,bot_id='100')


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_offset_is_monotonic_protected_and_owned_by_current_lease(native_context):
    from fastapi import HTTPException
    from app.models import ChannelInstallationLease
    c=native_context;s=gate(c);assert s.load_cursor()['offset']==0;s.advance(17);assert s.load_cursor()['offset']==18;s.advance(16);assert s.load_cursor()['offset']==18
    with c.sessions() as db:assert 'ciphertext' in db.get(ChannelInstallationLease,c.leases[0][0]).cursor_json['telegram_polling']
    other=import_module('app.services.channel_connection_telegram_polling').TelegramState(c.sessions,lease_id=c.leases[0][0],owner='other',fence=c.leases[0][1],bot_id='100')
    with pytest.raises(HTTPException):other.advance(99)
    assert s.load_cursor()['offset']==18


def pending(c):
    from app.models import ChannelConnectionDelivery
    did=accept(c,event());s=gate(c)
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);s.write_context(item,{'callback_id':'synthetic-callback','telegram_ack':{'status':'pending','expires_at':state.clock(db).timestamp()+10}});db.commit()
    return s,did


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
@pytest.mark.parametrize('outcome',['confirmed','unknown','rejected'])
def test_callback_reserved_once_and_only_confirmed_receipt_can_execute(native_context,outcome):
    from app.models import ChannelConnectionDelivery
    c=native_context;s,did=pending(c);lid,fence=c.leases[0];cid=c.ids['connections'][0]
    reservation=s.reserve_ack(did);assert reservation and 'synthetic-callback' not in repr(reservation)
    assert s.reserve_ack(did) is None
    with c.sessions() as db:assert state.claim_delivery(db,cid,lid,c.owner,fence,deadline_seconds=30,can_execute=s.can_execute) is None
    assert s.finish_ack(did,reservation.claim_id,outcome)
    assert not s.finish_ack(did,reservation.claim_id,'confirmed') and s.reserve_ack(did) is None
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);assert 'callback_id' not in s.context(item)
        assert s.can_execute(item)==(outcome=='confirmed') and item.status==({'confirmed':'received','unknown':'unknown','rejected':'failed'}[outcome])


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_callback_crash_is_never_reissued_and_generation_change_cancels(native_context):
    from app.models import ChannelConnection,ChannelConnectionDelivery
    c=native_context;s,did=pending(c);s.reserve_ack(did)
    with c.sessions() as db:
        item=db.get(ChannelConnectionDelivery,did);ctx=s.context(item);ctx['telegram_ack']['expires_at']=0;s.write_context(item,ctx);db.commit()
    with c.sessions() as db:item=db.get(ChannelConnectionDelivery,did);assert not s.can_execute(item);db.commit()
    with c.sessions() as db:assert db.get(ChannelConnectionDelivery,did).status=='unknown'
    assert s.reserve_ack(did) is None

@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_prepare_keeps_active_target_connected_and_restores_generation_cutoff(native_context):
    from types import SimpleNamespace
    from app.models import ChannelConnection,ChannelInstallationLease
    c=native_context;s=gate(c);cid=c.ids['connections'][0];lid,fence=c.leases[0];scope='telegram-bot:100'
    with c.sessions() as db:
        row=db.get(ChannelConnection,cid);row.listener_scope_hash=state.scoped_hash('verified-listener:telegram:'+scope);db.get(ChannelInstallationLease,lid).listener_scope_hash=row.listener_scope_hash;generation=row.generation;db.commit()
    target=SimpleNamespace(spec=SimpleNamespace(id=cid,generation=generation),listener_scope=scope)
    boundary=s.prepare_targets([target]);assert boundary>0;s.mark_ready([target])
    with c.sessions() as db:
        row=db.get(ChannelConnection,cid);cutoff=row.accept_after;state.activate_connection(db,cid,lid,c.owner,fence,{'installation_id':row.installation_id,'account_id':row.account_id,'listener_scope':scope});db.commit()
    assert s.prepare_targets([target])==0
    with c.sessions() as db:
        row=db.get(ChannelConnection,cid);assert row.observed_state=='connected' and row.accept_after==cutoff


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_long_idle_resets_once_before_poll_and_preserves_unknown(native_context):
    from datetime import timedelta
    from app.models import ChannelInstallationLease,ChannelConnectionDelivery,AuditLog
    from sqlalchemy import select
    c=native_context;s=gate(c);s.advance(9000)
    s,did=pending(c);reservation=s.reserve_ack(did);s.finish_ack(did,reservation.claim_id,'unknown')
    with c.sessions() as db:
        lease=db.get(ChannelInstallationLease,c.leases[0][0]);v=s._load(lease)
        v['last_commit_at']=(state.clock(db)-timedelta(days=8)).isoformat();s._save(lease,v);db.commit()
    first=s.poll_cursor();assert first['offset']==0 and first['epoch']==1
    assert s.poll_cursor()['epoch']==1
    recreated=gate(c);assert recreated.poll_cursor()['offset']==0 and recreated.poll_cursor()['epoch']==1
    recreated.advance(3);assert recreated.poll_cursor()['offset']==4 and recreated.poll_cursor()['epoch']==1
    with c.sessions() as db:
        assert db.get(ChannelConnectionDelivery,did).status=='unknown'
        audits=list(db.scalars(select(AuditLog).where(AuditLog.target_id==c.leases[0][0],AuditLog.action_type=='system.connection.telegram_idle_reset')))
        assert len(audits)==1 and audits[0].after_json['reason']=='TELEGRAM_LONG_IDLE'
        for audit in audits:db.delete(audit)
        db.commit()


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_short_idle_and_duplicate_commit_do_not_extend_idle_window(native_context):
    from datetime import timedelta
    from app.models import ChannelInstallationLease
    c=native_context;s=gate(c);s.advance(30)
    with c.sessions() as db:
        lease=db.get(ChannelInstallationLease,c.leases[0][0]);v=s._load(lease)
        previous=(state.clock(db)-timedelta(days=6)).isoformat();v['last_commit_at']=previous;s._save(lease,v);db.commit()
    assert s.poll_cursor()['offset']==31 and s.poll_cursor()['epoch']==0
    s.advance(29);assert s.load_cursor()['last_commit_at']==previous
