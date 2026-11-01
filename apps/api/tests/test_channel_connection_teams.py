from importlib import import_module
import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from tests.test_channel_connection_supervisor import native_context,test_engine,migrated_test_database,prepare,worker,Adapter
from tests.test_channel_connections import connection_api


def test_pending_first_activity_is_connecting_without_false_connected_or_error(native_context):
    async def run():
        from app.services import channel_connection_supervisor as common
        class Pending(Adapter):
            async def verify(self,spec,credentials):raise common.NativeHandshakePending()
        c=native_context;prepare(c);a=worker(c,Pending())
        try:
            await a.step()
            from app.models import ChannelConnection
            with c.sessions() as db:
                row=db.get(ChannelConnection,c.ids['connections'][0])
                assert row.observed_state=='connecting' and row.last_error_code=='WAITING_FIRST_ACTIVITY'
            assert not a.runs
        finally:await a.stop()
    asyncio.run(run())


def test_exclusive_verified_app_cannot_have_two_organization_receivers(connection_api):
    from app.models import Group,Bot,ChannelConnection
    from app.services import channel_connection_state as state
    c=connection_api
    item=c.client.post(c.base,json=c.payload).json()['data']['item']
    from uuid import UUID
    a=c.db.get(ChannelConnection,UUID(item['id']));a.desired_state='connected'
    group=Group(organization_id=c.foreign.organization_id,code='C03-other',name='C03-other');c.db.add(group);c.db.flush()
    bot=Bot(organization_id=c.foreign.organization_id,group_id=group.id,name='C03-other');c.db.add(bot);c.db.flush()
    b=ChannelConnection(organization_id=c.foreign.organization_id,channel_id=c.foreign.id,bot_id=bot.id,name='C03-other',provider=a.provider,installation_id=a.installation_id,account_id=a.account_id,target_id='other',desired_state='connected');c.db.add(b);c.db.commit()
    first=state.claim_installation(c.db,a,'owner-a',ttl_seconds=30,listener_scope='c03-same-physical-app',exclusive=True);c.db.commit()
    assert state.claim_installation(c.db,b,'owner-b',ttl_seconds=30,listener_scope='c03-same-physical-app',exclusive=True) is None
    c.db.commit();first.lease_expires_at=state.clock(c.db)-timedelta(seconds=1);c.db.commit()
    assert state.claim_installation(c.db,b,'owner-b',ttl_seconds=30,listener_scope='c03-same-physical-app',exclusive=True) is not None


def test_first_signed_activity_during_connecting_is_not_treated_as_old_history(native_context):
    from app.models import ChannelConnection
    from app.services import channel_connection_state as state
    c=native_context;cid=c.ids['connections'][0];lease,fence=c.leases[0]
    with c.sessions() as db:
        row=db.get(ChannelConnection,cid);row.observed_state='connecting';row.accept_after=state.clock(db)-timedelta(seconds=5);cutoff=row.accept_after;db.commit()
        state.activate_connection(db,cid,lease,c.owner,fence,{'installation_id':row.installation_id,'account_id':row.account_id});db.commit()
        from tests.test_channel_connection_runtime import event
        item=state.accept_delivery(db,cid,lease,c.owner,fence,{**event(),'occurredAt':(cutoff+timedelta(seconds=1)).isoformat()})
        assert item.status=='received' and row.accept_after==cutoff


def test_connect_records_server_cutoff_before_first_activity(connection_api,monkeypatch):
    from uuid import UUID
    from app.models import ChannelConnection
    from app.core.config import settings
    from app.services.channel_connection_supervisor import ADAPTERS
    c=connection_api;monkeypatch.setattr(settings,'channel_connection_worker_enabled',True);monkeypatch.setitem(ADAPTERS,'slack',Adapter())
    item=c.client.post(c.base,json=c.payload).json()['data']['item'];assert c.client.post(c.base+'/'+item['id']+'/connect').status_code==200
    assert c.db.get(ChannelConnection,UUID(item['id'])).accept_after is not None


def test_known_provider_scope_error_keeps_only_fixed_code(native_context):
    async def run():
        from app.services import channel_connection_supervisor as common
        from fastapi import HTTPException
        class Conflict(Adapter):
            async def verify(self,spec,credentials):raise HTTPException(409,'TEAMS_APP_SCOPE_IN_USE: synthetic-secret-must-not-appear')
        c=native_context;prepare(c);a=worker(c,Conflict())
        try:
            await a.step()
            from app.models import ChannelConnection
            with c.sessions() as db:
                row=db.get(ChannelConnection,c.ids['connections'][0]);assert row.last_error_code=='TEAMS_APP_SCOPE_IN_USE'
        finally:await a.stop()
    asyncio.run(run())
