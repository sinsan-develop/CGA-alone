from importlib import import_module
import asyncio
from datetime import datetime,timezone,timedelta
from uuid import UUID,uuid4
import pytest
from fastapi import HTTPException,FastAPI
from sqlalchemy import select
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_teams_auth import signed
from tests.test_channel_connection_supervisor import drain


def subject():return import_module('app.services.channel_connection_teams')


class Http:
    def __init__(self):self.oauth_calls=[];self.posts=[];self.fail=False
    def oauth(self,tenant,client,secret):
        self.oauth_calls.append((tenant,client));assert secret=='synthetic-only'
        return {'access_token':'synthetic-access-token','token_type':'Bearer','expires_in':3600}
    def send(self,service,conversation,activity,token,packet):
        assert service=='https://smba.trafficmanager.net/amer/' and token=='synthetic-access-token'
        self.posts.append((service,conversation,activity,packet))
        if self.fail:raise TimeoutError('synthetic transport result unknown')
        return {'id':'synthetic-post-'+str(len(self.posts))}


def setup(c,signed):
    from app.models import ChannelConnection,ChannelInstallationLease,AdminChannel
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.services.channel_connection_teams_auth import TeamsAuthenticator
    from app.core.config import settings
    _,client,_,_,_,_,transport,_=signed;tenant=str(uuid4());http=Http()
    with c.sessions() as db:
        db.get(AdminChannel,c.ids['parent_id']).code='TEAMS'
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id=tenant;row.account_id=client;row.target_id='teams-target-'+str(i)
            row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)-timedelta(seconds=1)
            row.credential_ciphertext=encrypt_credentials({'client_id':client,'tenant_id':tenant,'client_secret':'synthetic-only'},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    adapter=subject().TeamsAdapter(c.sessions,owner=c.owner,http=http,authenticator=TeamsAuthenticator(transport=transport))
    return ConnectionSupervisor(c.sessions,{'ms_teams':adapter},owner=c.owner),adapter,http,tenant,client


def activity(tenant,client,event='teams-event-1',index=0):
    return {'type':'message','id':event,'channelId':'msteams','timestamp':datetime.now(timezone.utc).isoformat(),'serviceUrl':'https://smba.trafficmanager.net/amer/',
        'channelData':{'tenant':{'id':tenant}},'conversation':{'id':'teams-target-'+str(index),'conversationType':'groupChat'},'from':{'id':'29:synthetic-user','name':'Teams QA'},'recipient':{'id':'28:'+client},
        'text':'<at>QA bot</at> 검증 질문','entities':[{'type':'mention','mentioned':{'id':'28:'+client},'text':'<at>QA bot</at>'}]}


async def handshake(a,adapter,header,body):
    task=asyncio.create_task(adapter.receive(header,body))
    for _ in range(40):
        await asyncio.sleep(.03);await a.step()
        if task.done():return await task
    raise AssertionError('first activity was not acknowledged')


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_signed_first_activity_activates_then_existing_fixed_qa_posts_once(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();assert not a.runs and len(http.oauth_calls)==1
            from app.models import ChannelConnection,ChannelConnectionDelivery
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connecting'
            body=activity(tenant,client);header='Bearer '+signed[-1]();first=await handshake(a,adapter,header,body)
            duplicate=await adapter.receive(header,body);assert first==duplicate
            await a.step();await drain(a);await a.step();await drain(a)
            assert len(http.posts)==1 and '정의한 로컬 답변' in str(http.posts[0])
            with c.sessions() as db:
                delivery=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert delivery.status=='sent' and delivery.queue_event_id and delivery.room_id
                assert delivery.context_ciphertext and 'synthetic-access-token' not in str(delivery.event_json)
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_wrong_tenant_target_recipient_and_bad_token_never_activate(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();header='Bearer '+signed[-1]();body=activity(tenant,client)
            for value in [{**body,'channelData':{'tenant':{'id':str(uuid4())}}},{**body,'conversation':{'id':'other-target','conversationType':'groupChat'}},{**body,'recipient':{'id':'28:'+str(uuid4())}}]:
                with pytest.raises(HTTPException):await adapter.receive(header,value)
            with pytest.raises(HTTPException):await adapter.receive('Bearer invalid',body)
            from app.models import ChannelConnection
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connecting'
            assert not http.posts
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_valid_general_chat_is_saved_but_does_not_post_a_bot_answer(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();body=activity(tenant,client);body.update(text='일반 대화',entities=[])
            first=await handshake(a,adapter,'Bearer '+signed[-1](),body)
            await a.step();await drain(a);await a.step();await drain(a)
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:
                delivery=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert delivery.status=='sent' and delivery.request_message_id and not delivery.result_json['botMessages']
            assert not http.posts
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_public_ingress_requires_enabled_worker_bounded_body_and_microsoft_jwt(native_context,signed,monkeypatch):
    async def run():
        import httpx
        from app.core.config import settings
        from app.services.channel_connection_supervisor import ADAPTERS
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        monkeypatch.setitem(ADAPTERS,'ms_teams',adapter)
        app=FastAPI();app.include_router(import_module('app.api.routes.channel_connection_events').router,prefix='/api/v1')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://synthetic.invalid') as browser:
            path='/api/v1/channel-connections/ms-teams/events'
            try:
                monkeypatch.setattr(settings,'channel_connection_worker_enabled',False)
                assert (await browser.post(path,json={})).status_code==503
                monkeypatch.setattr(settings,'channel_connection_worker_enabled',True)
                assert (await browser.post(path,json=activity(tenant,client))).status_code==401
                async def oversized():
                    yield b' '*(200*1024)
                    yield b' '*(100*1024)
                assert (await browser.post(path,content=oversized())).status_code==413
                await a.step()
                body=activity(tenant,client)
                pending=asyncio.create_task(browser.post(path,json=body,headers={'Authorization':'Bearer '+signed[-1]()}))
                for _ in range(40):
                    await asyncio.sleep(.03);await a.step()
                    if pending.done():break
                response=await pending;assert response.status_code==200 and response.json()=={}
                duplicate=await browser.post(path,json=body,headers={'Authorization':'Bearer '+signed[-1]()});assert duplicate.status_code==200
                from app.models import ChannelConnectionDelivery
                with c.sessions() as db:assert len(list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]))))==1
            finally:await a.stop()
    asyncio.run(run())


def test_oauth_and_reply_transport_use_fixed_destinations_and_encode_identifiers():
    calls=[]
    def post(url,headers,body):calls.append((url,headers,body));return {'id':'synthetic'}
    m=subject();http=m.TeamsHttpTransport(post=post);tenant=str(uuid4());client=str(uuid4())
    http.oauth(tenant,client,'synthetic-secret');assert calls[0][0]=='https://login.microsoftonline.com/'+tenant+'/oauth2/v2.0/token'
    assert 'synthetic-secret' not in calls[0][0] and b'client_secret=synthetic-secret' in calls[0][2]
    http.send('https://smba.trafficmanager.net/amer/','conversation/with?query','activity/#target','synthetic-token',{'type':'message','text':'QA'})
    assert calls[1][0]=='https://smba.trafficmanager.net/amer/v3/conversations/conversation%2Fwith%3Fquery/activities/activity%2F%23target'
    assert calls[1][1]['Authorization']=='Bearer synthetic-token'
    with pytest.raises(HTTPException):http.send('https://attacker.invalid/','c','a','synthetic-token',{})
    assert len(calls)==2


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_malformed_signed_activity_cannot_leave_a_handshake_proof(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();header='Bearer '+signed[-1]();body=activity(tenant,client)
            with pytest.raises(HTTPException):await adapter.receive(header,{**body,'timestamp':'invalid'})
            from app.models import ChannelInstallationLease
            with c.sessions() as db:
                leases=list(db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])))
                assert all(not (l.cursor_json or {}).get('teams_proofs') for l in leases)
            with pytest.raises(HTTPException) as error:await adapter.receive(header,{**body,'channelData':[]})
            assert error.value.status_code in {403,422}
        finally:await a.stop()
    asyncio.run(run())
