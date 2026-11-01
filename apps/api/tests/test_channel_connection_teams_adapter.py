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


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_second_question_while_first_answer_is_not_sent_is_not_queued_to_execute(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();header='Bearer '+signed[-1]();first=await handshake(a,adapter,header,activity(tenant,client))
            second_body=activity(tenant,client,event='second-while-busy');second_body['from']={'id':'29:another-user','name':'Teams QA other'}
            second=await adapter.receive(header,second_body)
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:
                item=db.get(ChannelConnectionDelivery,UUID(second['delivery_id']));assert item.status=='failed' and item.last_error_code=='ROOM_BUSY'
            await a.step();await drain(a);await a.step();await drain(a)
            assert len(http.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_native_card_checks_latest_source_and_owner_before_resuming_scenario(native_context,signed):
    async def run():
        from copy import deepcopy
        from app.models import BotVersion,ChannelConnectionDelivery
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        with c.sessions() as db:
            version=db.get(BotVersion,c.ids['version_id']);doc=deepcopy(version.version_json);graph=doc['dialog_flow_graphs'][1]
            talk=next(n for n in graph['nodes'] if n['kind']=='talk');talk['config']={'messageType':'button','basicMessages':['선택하세요'],'messages':['확인','다음'],'responseType':'relay','responseVariableName':'choice'}
            graph['nodes'].append({'id':'teams-result','kind':'talk','config':{'messageType':'text','basicMessages':['선택: {{$choice}}'],'responseType':'none'}})
            for link in graph['links']:
                if link['sourceNodeId']==talk['id']:link['targetNodeId']='teams-result'
            graph['links'].append({'sourceNodeId':'teams-result','sourcePort':'next','targetNodeId':'local-end'});version.version_json=doc;db.commit()
        try:
            await a.step();header='Bearer '+signed[-1]();await handshake(a,adapter,header,activity(tenant,client))
            await a.step();await drain(a);await a.step();await drain(a)
            data=http.posts[0][3]['attachments'][0]['content']['actions'][1]['data']
            stale=activity(tenant,client,event='stale-card');stale.update(text='',entities=[],value={**data,'sourceMessageId':str(uuid4())})
            bad=await adapter.receive(header,stale);await a.step();await drain(a)
            with c.sessions() as db:
                item=db.get(ChannelConnectionDelivery,UUID(bad['delivery_id']));assert item.status=='failed' and item.last_error_code=='EXECUTION_NOT_STARTED'
            wrong=activity(tenant,client,event='wrong-card-owner');wrong.update(text='',entities=[],value=data);wrong['from']={'id':'29:wrong-owner','name':'Other QA'}
            rejected=await adapter.receive(header,wrong);await a.step();await drain(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(rejected['delivery_id'])).status=='failed'
            own=activity(tenant,client,event='correct-card-owner');own.update(text='',entities=[],value=data)
            await adapter.receive(header,own);await a.step();await drain(a);await a.step();await drain(a)
            assert len(http.posts)==2 and '선택: 다음' in str(http.posts[-1])
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_complete_native_packet_is_preflighted_before_any_post(native_context,signed):
    async def run():
        from copy import deepcopy
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();body=activity(tenant,client);body['channelData']['channel']={'id':'teams-target-0'};body['conversation']={'id':'🚀'*1700,'conversationType':'channel'}
            first=await handshake(a,adapter,'Bearer '+signed[-1](),body);await a.step();await drain(a)
            with c.sessions() as db:
                row=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert row.status=='ready';result=deepcopy(row.result_json);result['botMessages'][0]['text']='x'*23000;row.result_json=result;db.commit()
            await a.step();await drain(a)
            with c.sessions() as db:
                row=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert row.status=='failed' and row.last_error_code=='DELIVERY_NOT_SUBMITTED'
                assert row.result_json['botMessages']
            assert not http.posts
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_adaptive_execute_invoke_is_durable_and_automatic_refresh_is_not_a_question(native_context,signed,monkeypatch):
    async def run():
        import httpx
        from app.core.config import settings
        from app.services.channel_connection_supervisor import ADAPTERS
        from app.models import ChannelConnectionDelivery,ChannelConnection
        c=native_context;a,adapter,http,tenant,client=setup(c,signed);monkeypatch.setitem(ADAPTERS,'ms_teams',adapter);monkeypatch.setattr(settings,'channel_connection_worker_enabled',True)
        app=FastAPI();app.include_router(import_module('app.api.routes.channel_connection_events').router,prefix='/api/v1')
        headers={'Authorization':'Bearer '+signed[-1]()};path='/api/v1/channel-connections/ms-teams/events'
        wire={'webchatRichFormVersion':'1.0','sourceMessageId':str(uuid4()),'sourceTalkNodeId':'old-card','response':{'buttonValue':'확인'}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://synthetic.invalid') as browser:
            try:
                await a.step();automatic=activity(tenant,client,event='automatic-card-refresh');automatic.update(type='invoke',name='adaptiveCard/action',value={'trigger':'automatic','action':{'type':'Action.Execute','data':wire}})
                response=await browser.post(path,json=automatic,headers=headers)
                assert response.status_code==200 and response.json()['type']=='application/vnd.microsoft.activity.message'
                with c.sessions() as db:
                    assert not list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])))
                    assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connecting'
                await handshake(a,adapter,headers['Authorization'],activity(tenant,client))
                manual=activity(tenant,client,event='manual-card-action');manual.update(type='invoke',name='adaptiveCard/action',value={'trigger':'manual','action':{'type':'Action.Execute','data':wire}})
                response=await browser.post(path,json=manual,headers=headers)
                assert response.status_code==200 and response.json()['statusCode']==200
                with c.sessions() as db:
                    assert len(list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]))))==2
            finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_other_api_replica_can_commit_signed_activity_to_the_lease_owner(native_context,signed):
    async def run():
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        other=subject().TeamsAdapter(c.sessions,owner='c03-other-api-replica',http=Http(),authenticator=adapter.authenticator)
        try:
            await a.step();result=await handshake(a,other,'Bearer '+signed[-1](),activity(tenant,client))
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(result['delivery_id'])).status in {'received','executing','ready'}
            assert not other.http.oauth_calls
            await a.step();await drain(a);await a.step();await drain(a)
            assert len(http.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_same_app_two_targets_disconnect_independently(native_context,signed):
    async def run():
        from app.models import ChannelConnection
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,http,tenant,client=setup(c,signed)
        try:
            await a.step();header='Bearer '+signed[-1]()
            await handshake(a,adapter,header,activity(tenant,client));await a.step();await drain(a);await a.step();await drain(a)
            await handshake(a,adapter,header,activity(tenant,client,event='target-b-first',index=1));await a.step();await drain(a);await a.step();await drain(a)
            assert len(a.runs)==1 and len(next(iter(a.runs.values())).targets)==2 and len(http.oauth_calls)==1
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step();assert len(next(iter(a.runs.values())).targets)==1
            with pytest.raises(HTTPException):await adapter.receive(header,activity(tenant,client,event='off-a'))
            await adapter.receive(header,activity(tenant,client,event='still-on-b',index=1));await a.step();await drain(a);await a.step();await drain(a)
            assert len(http.posts)==3 and http.posts[-1][1]=='teams-target-1'
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_provider_timeout_remains_unknown_without_native_resend(native_context,signed):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,http,tenant,client=setup(c,signed);http.fail=True
        try:
            await a.step();first=await handshake(a,adapter,'Bearer '+signed[-1](),activity(tenant,client))
            await a.step();await drain(a);await a.step();await drain(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(first['delivery_id'])).status=='unknown'
            for _ in range(3):await a.step();await drain(a)
            assert len(http.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['ms_teams'],indirect=True)
def test_restart_recovers_verified_scope_and_does_not_resend_uncertain_post(native_context,signed):
    async def run():
        from app.models import ChannelConnectionDelivery,ChannelConnection
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        from app.services import channel_connection_state as state
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,http,tenant,client=setup(c,signed);b=None
        try:
            await a.step();first=await handshake(a,adapter,'Bearer '+signed[-1](),activity(tenant,client));await a.step();await drain(a)
            with c.sessions() as db:
                row=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert row.status=='ready';row.status='sending';row.send_claim_id=str(uuid4());row.deadline_at=state.clock(db)-timedelta(seconds=1)
                disconnect_connection(db.get(ChannelConnection,c.ids['connections'][1]));db.commit()
            await a.stop()
            new=subject().TeamsAdapter(c.sessions,owner='c03-restarted-api',http=http,authenticator=adapter.authenticator);b=ConnectionSupervisor(c.sessions,{'ms_teams':new},owner='c03-restarted-api')
            for _ in range(3):await b.step();await drain(b)
            with c.sessions() as db:
                assert db.get(ChannelConnectionDelivery,UUID(first['delivery_id'])).status=='unknown'
                assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connected'
                assert db.get(ChannelConnection,c.ids['connections'][1]).desired_state=='disconnected'
            assert not http.posts
        finally:
            if not a.stopping:await a.stop()
            if b:await b.stop()
    asyncio.run(run())
