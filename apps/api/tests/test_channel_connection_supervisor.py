from importlib import import_module
import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from tests.test_channel_connection_runtime import native_context,event as base_event,test_engine,migrated_test_database
from tests.test_channel_connections import connection_api


def event():return {**base_event(),'channelId':'CQA001'}

def module():return import_module('app.services.channel_connection_supervisor')


class Listener:
    def __init__(self,targets):self.targets=targets;self.closed=False;self.sent=[];self.fail=False
    async def update(self,targets):self.targets=targets
    async def listen(self,accept):await asyncio.Event().wait()
    async def send(self,target,delivery_id,packets,context):
        self.sent.append((delivery_id,packets,context))
        if self.fail:raise TimeoutError('synthetic-transport-result-unknown')
        return ['synthetic-receipt']
    async def close(self):self.closed=True


class Adapter:
    def __init__(self):self.listeners=[];self.bad_identity=False
    async def verify(self,spec,credentials):
        m=module()
        return m.VerifiedTarget(spec, 'wrong' if self.bad_identity else spec.installation_id,spec.account_id,'synthetic-app-scope')
    async def open(self,targets):
        listener=Listener(targets);self.listeners.append(listener);return listener


def prepare(c):
    from app.models import ChannelConnection,ChannelInstallationLease
    from app.services.channel_connections import encrypt_credentials
    from app.core.config import settings
    with c.sessions() as db:
        for cid in c.ids['connections']:
            row=db.get(ChannelConnection,cid);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None
            row.credential_ciphertext=encrypt_credentials({'bot_token':'synthetic-only'},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=module().state.clock(db)-timedelta(seconds=1)
        db.commit()


async def drain(worker):
    for _ in range(200):
        await asyncio.sleep(.02)
        if not worker.jobs:return
    raise AssertionError('worker did not settle')


def worker(c,adapter,owner=None):return module().ConnectionSupervisor(c.sessions,{'slack':adapter},owner=owner or 'c02-supervisor-'+uuid4().hex,max_jobs=4,request=c.request)


def test_shared_listener_single_owner_disconnect_is_independent(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter,'owner-a');b=worker(c,adapter,'owner-b')
        try:
            await a.step();await b.step()
            assert len(adapter.listeners)==1 and len(a.runs)==1 and not b.runs
            from app.models import ChannelConnection
            from app.services.channel_connections import disconnect_connection
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step()
            assert not adapter.listeners[0].closed
            assert [t.spec.id for t in adapter.listeners[0].targets]==[c.ids['connections'][1]]
        finally:await a.stop();await b.stop()
    asyncio.run(run())


def test_supervisor_real_fixed_qa_sdk_packets_and_dedup(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        try:
            await a.step();run_id=next(iter(a.runs));m=module()
            envelope=m.InboundEnvelope(c.ids['connections'][0],event(),{'receipt_hint':'synthetic-private'})
            ids=await a.ingest(run_id,[envelope]);assert await a.ingest(run_id,[envelope])==ids
            await a.step();await drain(a);await a.step();await drain(a)
            assert len(adapter.listeners[0].sent)==1
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:
                item=db.get(ChannelConnectionDelivery,ids[0]);assert item.status=='sent' and item.queue_event_id and item.room_id
                assert 'synthetic-private' not in str(item.event_json) and item.context_ciphertext
            assert '정의한 로컬 답변' in str(adapter.listeners[0].sent[0][1])
            await a.step();await drain(a);assert len(adapter.listeners[0].sent)==1
        finally:await a.stop()
    asyncio.run(run())


def test_post_timeout_is_unknown_and_never_resubmitted(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        try:
            await a.step();adapter.listeners[0].fail=True
            ids=await a.ingest(next(iter(a.runs)),[module().InboundEnvelope(c.ids['connections'][0],event())])
            await a.step();await drain(a);await a.step();await drain(a)
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,ids[0]).status=='unknown'
            for _ in range(3):await a.step();await drain(a)
            assert len(adapter.listeners[0].sent)==1
        finally:await a.stop()
    asyncio.run(run())


def test_shutdown_preserves_desired_restart_and_disconnected_stays_off(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        from app.models import ChannelConnection
        from app.services.channel_connections import disconnect_connection
        await a.step();await a.stop()
        with c.sessions() as db:
            assert db.get(ChannelConnection,c.ids['connections'][0]).desired_state=='connected'
            disconnect_connection(db.get(ChannelConnection,c.ids['connections'][1]));db.commit()
        b=worker(c,adapter)
        try:
            await b.step();assert len(b.runs)==1
            assert [t.spec.id for t in adapter.listeners[-1].targets]==[c.ids['connections'][0]]
        finally:await b.stop()
    asyncio.run(run())


def test_bad_verified_identity_cannot_open_listener(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();adapter.bad_identity=True;a=worker(c,adapter)
        try:
            await a.step();assert not a.runs and not adapter.listeners
            from app.models import ChannelConnection
            with c.sessions() as db:
                item=db.get(ChannelConnection,c.ids['connections'][0]);assert item.observed_state=='error' and item.last_error_code=='CONNECTION_VERIFICATION_FAILED'
        finally:await a.stop()
    asyncio.run(run())


def test_worker_never_ingests_disconnected_or_other_connection(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        try:
            await a.step();run_id=next(iter(a.runs));m=module()
            with pytest.raises(Exception):await a.ingest(run_id,[m.InboundEnvelope(uuid4(),event())])
            from app.models import ChannelConnection
            from app.services.channel_connections import disconnect_connection
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            with pytest.raises(Exception):await a.ingest(run_id,[m.InboundEnvelope(c.ids['connections'][0],event())])
        finally:await a.stop()
    asyncio.run(run())


def test_unregistered_disabled_or_keyless_execution_is_unavailable(monkeypatch):
    m=module();from app.core.config import settings
    monkeypatch.setattr(settings,'channel_connection_worker_enabled',False,raising=False)
    monkeypatch.setitem(m.ADAPTERS,'slack',Adapter())
    monkeypatch.setattr(settings,'channel_connection_encryption_key','synthetic-not-read')
    assert not m.execution_available('slack')
    monkeypatch.setattr(settings,'channel_connection_worker_enabled',True)
    assert m.execution_available('slack') and not m.execution_available('discord')
    monkeypatch.setattr(settings,'channel_connection_encryption_key','')
    assert not m.execution_available('slack')


def test_connect_command_is_idempotent_and_only_requests_observation(connection_api,monkeypatch):
    c=connection_api;m=module();from app.core.config import settings
    monkeypatch.setattr(settings,'channel_connection_worker_enabled',True,raising=False)
    monkeypatch.setitem(m.ADAPTERS,'slack',Adapter())
    payload={**c.payload,'credentials':{'bot_token':'synthetic-only'}}
    created=c.client.post(c.base,json=payload);assert created.status_code==200
    cid=created.json()['data']['item']['id']
    first=c.client.post(c.base+'/'+cid+'/connect');assert first.status_code==200
    item=first.json()['data']['item'];assert item['desired_state']=='connected' and item['observed_state']=='connecting' and not item['identity_verified']
    second=c.client.post(c.base+'/'+cid+'/connect');assert second.status_code==200
    assert second.json()['data']['item']['generation']==item['generation']
    assert c.client.get(c.base).json()['data']['execution_available'] is True
    assert c.client.post(c.base+'/'+cid+'/disconnect').status_code==200
    assert c.client.post(c.base+'/'+cid+'/connect').json()['data']['item']['generation']>item['generation']


def test_listener_heartbeat_renews_without_reverification_and_prunes_off_target(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        from app.models import ChannelConnection,ChannelInstallationLease
        from app.services.channel_connections import disconnect_connection
        try:
            await a.step();r=next(iter(a.runs.values()))
            with c.sessions() as db:
                disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]))
                lease=db.get(ChannelInstallationLease,r.lease_id);lease.lease_expires_at=module().state.clock(db)+timedelta(seconds=2);db.commit()
            assert await a.heartbeat_once(r)
            with c.sessions() as db:
                lease=db.get(ChannelInstallationLease,r.lease_id)
                assert lease.lease_expires_at>module().state.clock(db)+timedelta(seconds=30)
                assert db.get(ChannelConnection,c.ids['connections'][1]).lease_expires_at==lease.lease_expires_at
            await a.step();assert not adapter.listeners[0].closed
        finally:await a.stop()
    asyncio.run(run())


def test_disabled_startup_does_not_create_worker_or_access_database(monkeypatch):
    async def run():
        from app.core.config import settings
        from app import main
        monkeypatch.setattr(settings,'channel_connection_worker_enabled',False)
        app=SimpleNamespace(state=SimpleNamespace())
        await main.start_connection_supervisor(app)
        assert not hasattr(app.state,'connection_supervisor')
        await main.stop_connection_supervisor(app)
    asyncio.run(run())


def test_default_startup_request_executes_first_native_question(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=module().ConnectionSupervisor(c.sessions,{'slack':adapter},owner='default-startup-qa')
        try:
            await a.step();ids=await a.ingest(next(iter(a.runs)),[module().InboundEnvelope(c.ids['connections'][0],event())])
            await a.step();await drain(a);await a.step();await drain(a)
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,ids[0]).status=='sent'
            assert len(adapter.listeners[0].sent)==1
        finally:await a.stop()
    asyncio.run(run())


def test_old_ready_after_disconnect_cannot_starve_new_generation(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter);m=module()
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        try:
            await a.step();ids=await a.ingest(next(iter(a.runs)),[m.InboundEnvelope(c.ids['connections'][0],event())])
            await a.step();await drain(a)
            with c.sessions() as db:
                assert db.get(ChannelConnectionDelivery,ids[0]).status=='ready'
                row=db.get(ChannelConnection,c.ids['connections'][0]);disconnect_connection(row);row.desired_state='connected';row.observed_state='connecting';row.generation+=1;db.commit()
            await a.step();new=await a.ingest(next(iter(a.runs)),[m.InboundEnvelope(c.ids['connections'][0],{**event(),'eventId':'new-generation-event'})])
            for _ in range(3):await a.step();await drain(a)
            with c.sessions() as db:
                old=db.get(ChannelConnectionDelivery,ids[0]);assert old.status=='cancelled' and old.result_json.get('botMessages')
                assert db.get(ChannelConnectionDelivery,new[0]).status=='sent'
            assert len(adapter.listeners[0].sent)==1
        finally:await a.stop()
    asyncio.run(run())


def test_inactive_target_does_not_close_valid_shared_listener(native_context):
    async def run():
        c=native_context;prepare(c);adapter=Adapter();a=worker(c,adapter)
        from app.models import AdminChannel,ChannelConnection
        extra=None
        try:
            with c.sessions() as db:
                original=db.get(AdminChannel,c.ids['parent_id']);extra=AdminChannel(organization_id=original.organization_id,code='C02_EXTRA',name='C02 inactive',data_json=dict(original.data_json));db.add(extra);db.flush();eid=extra.id
                db.get(ChannelConnection,c.ids['connections'][0]).channel_id=eid;db.commit()
            await a.step();listener=adapter.listeners[0]
            with c.sessions() as db:db.get(AdminChannel,eid).status='inactive';db.commit()
            await a.step();assert len(a.runs)==1 and not listener.closed
            ids=await a.ingest(next(iter(a.runs)),[module().InboundEnvelope(c.ids['connections'][1],{**event(),'message':'일반 대화','addressed':False})])
            await a.step();await drain(a);await a.step();await drain(a)
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:
                assert db.get(ChannelConnectionDelivery,ids[0]).status=='sent'
                assert db.get(ChannelConnection,c.ids['connections'][1]).observed_state=='connected'
        finally:
            await a.stop()
            if extra:
                with c.sessions() as db:db.get(ChannelConnection,c.ids['connections'][0]).channel_id=c.ids['parent_id'];db.flush();db.delete(db.get(AdminChannel,eid));db.commit()
    asyncio.run(run())
