import asyncio,json,time
from datetime import datetime,timezone,timedelta
from uuid import UUID
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_supervisor import drain
from tests.test_channel_connection_teams_adapter import handshake
from tests.test_channel_connection_google_chat_auth import signing,token
from tests.test_channel_connection_google_chat import credentials


class Http:
    def __init__(self):self.posts=[];self.fail=False
    def oauth(self,identity):return {'access_token':'synthetic-access','token_type':'Bearer','expires_in':3600}
    def get(self,path,token):
        if path.endswith('/members/app'):return {'name':path[:-3]+'100','state':'JOINED','member':{'name':'users/100','type':'BOT'}}
        return {'name':path,'spaceType':'SPACE'}
    def send(self,space,token,body,request_id,reply_option):
        self.posts.append((space,body,request_id,reply_option))
        if self.fail:raise TimeoutError('synthetic uncertain post')
        return {'name':space+'/messages/out'+str(len(self.posts))}


def setup(c,signing,credentials):
    from app.models import ChannelConnection,ChannelInstallationLease
    from app.core.config import settings
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.services.channel_connection_google_chat import GoogleChatAdapter
    from app.services.channel_connection_google_chat_auth import GoogleChatAuthenticator
    h=Http()
    with c.sessions() as db:
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id='1234567890';row.account_id='users/100';row.target_id='spaces/QA'+str(i);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)-timedelta(seconds=1)
            row.credential_ciphertext=encrypt_credentials(credentials,row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    adapter=GoogleChatAdapter(c.sessions,owner=c.owner,http=h,authenticator=GoogleChatAuthenticator(transport=lambda _: {'synthetic-key':signing[1]}))
    return ConnectionSupervisor(c.sessions,{'google_chat':adapter},owner=c.owner),adapter,h,'Bearer '+token(signing[0])


def event(index=0,identifier='m1',actor='users/500',thread='T',addressed=True):
    space={'name':'spaces/QA'+str(index),'spaceType':'SPACE'};user={'name':actor,'type':'HUMAN'};text=('@CGA ' if addressed else '')+'검증 질문'
    return {'type':'MESSAGE','eventTime':datetime.now(timezone.utc).isoformat(),'space':space,'user':user,'message':{'name':space['name']+'/messages/'+identifier,'space':space,'thread':{'name':space['name']+'/threads/'+thread},'sender':user,'text':text,'annotations':[{'type':'USER_MENTION','startIndex':0,'length':5,'userMention':{'type':'MENTION','user':{'name':'users/100','type':'BOT'}}}] if addressed else []}}


async def finish(a):await a.step();await drain(a);await a.step();await drain(a)


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
@pytest.mark.parametrize('attempt',range(5))
def test_signed_first_event_commits_then_existing_qa_posts_once(native_context,signing,credentials,attempt,monkeypatch):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        import traceback,re
        import app.services.channel_connection_supervisor as supervisor
        errors=[];original=supervisor.execute_connection_delivery
        def traced(*args,**kwargs):
            try:return original(*args,**kwargs)
            except Exception as error:
                detail=getattr(error,'detail','');detail=detail if isinstance(detail,str) and re.fullmatch('[A-Z0-9_]{1,80}',detail) else ''
                errors.append((type(error).__name__,detail,getattr(getattr(error,'orig',None),'sqlstate',None),getattr(getattr(getattr(error,'orig',None),'diag',None),'message_primary',None),[(x.name,x.lineno) for x in traceback.extract_tb(error.__traceback__)]));raise
        monkeypatch.setattr(supervisor,'execute_connection_delivery',traced)
        try:
            await a.step();assert not a.runs
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connecting'
            body=event();first=await handshake(a,adapter,header,body);assert await adapter.receive(header,body)==first
            await finish(a)
            with c.sessions() as db:diagnostic=[(x.status,x.last_error_code) for x in db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]))]
            assert len(h.posts)==1 and h.posts[0][0]=='spaces/QA0' and '정의한 로컬 답변' in h.posts[0][1]['text'],(diagnostic,errors)
            with c.sessions() as db:
                item=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert item.status=='sent' and item.room_id and item.queue_event_id
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_unknown_post_never_retries_and_other_space_is_independent(native_context,signing,credentials):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);h.fail=True
        try:
            await a.step();first=await handshake(a,adapter,header,event());await finish(a)
            for _ in range(3):await finish(a)
            assert len(h.posts)==1
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(first['delivery_id'])).status=='unknown'
            h.fail=False;await handshake(a,adapter,header,event(1));await finish(a);assert len(h.posts)==2 and h.posts[-1][0]=='spaces/QA1'
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_wrong_signature_space_and_actor_cannot_execute(native_context,signing,credentials):
    async def run():
        from copy import deepcopy
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step()
            with pytest.raises(HTTPException):await adapter.receive('Bearer untrusted',event())
            with pytest.raises(HTTPException):await adapter.receive(header,event(8))
            forged=event();forged['message']['sender']={'name':'users/OTHER','type':'HUMAN'}
            with pytest.raises(HTTPException):await adapter.receive(header,forged)
            assert not h.posts and not a.runs
        finally:await a.stop()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_duplicate_receipt_does_not_wait_for_result_delivery_lock(native_context,signing,credentials,monkeypatch):
    async def run():
        import threading
        from app.services import channel_connection_state as state
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);held=threading.Event();release=threading.Event();original=state.mark_ready
        def holding(*args,**kwargs):
            result=original(*args,**kwargs);held.set()
            if not release.wait(5):raise AssertionError('controlled result lock deadline')
            return result
        monkeypatch.setattr(state,'mark_ready',holding)
        try:
            await a.step();body=event();first=await handshake(a,adapter,header,body);await a.step()
            assert await asyncio.to_thread(held.wait,3)
            try:assert await asyncio.wait_for(adapter.receive(header,body),.5)==first
            finally:release.set()
            await finish(a);assert len(h.posts)==1
        finally:release.set();await a.stop()
    asyncio.run(run())



def click(packet,*,actor='users/500',source=None):
    from copy import deepcopy
    buttons=next(w['buttonList']['buttons'] for w in packet['cardsV2'][0]['card']['sections'][0]['widgets'] if 'buttonList' in w)
    parameters=deepcopy(buttons[1]['onClick']['action']['parameters'])
    if source is not None:next(p for p in parameters if p['key']=='sourceMessageId')['value']=source
    frame=event(actor=actor);frame['type']='CARD_CLICKED';frame['message']['name']='spaces/QA0/messages/out1';frame['message']['sender']={'name':'users/100','type':'BOT'};frame['message'].pop('text');frame['message'].pop('annotations');frame['action']={'actionMethodName':'cga.select','parameters':parameters}
    return frame


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_card_source_actor_and_successful_redelivery_preserve_existing_variables(native_context,signing,credentials):
    from tests.test_channel_connection_telegram_runtime import buttons
    async def run():
        from uuid import uuid4
        c=native_context;buttons(c);a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step();await handshake(a,adapter,header,event());await finish(a);assert len(h.posts)==1
            packet=h.posts[0][1]
            for frame in [click(packet,source=str(uuid4())),click(packet,actor='users/501')]:await adapter.receive(header,frame);await finish(a)
            assert len(h.posts)==1
            good=click(packet);first=await adapter.receive(header,good);await finish(a);assert len(h.posts)==2 and '선택: 다음' in h.posts[-1][1]['text']
            assert await adapter.receive(header,good)==first;await finish(a);assert len(h.posts)==2
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_busy_is_not_queued_and_other_thread_gets_separate_room(native_context,signing,credentials):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step();first=await handshake(a,adapter,header,event());busy=await adapter.receive(header,event(identifier='m2',actor='users/501'))
            other=await adapter.receive(header,event(identifier='m3',thread='T2'))
            for _ in range(4):
                await finish(a)
                if len(h.posts)==2:break
            with c.sessions() as db:
                blocked=db.get(ChannelConnectionDelivery,UUID(busy['delivery_id']));assert blocked.status=='failed' and blocked.last_error_code=='ROOM_BUSY'
                one=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));two=db.get(ChannelConnectionDelivery,UUID(other['delivery_id']));assert one.room_id and two.room_id and one.room_id!=two.room_id
            assert len(h.posts)==2 and {p[1]['thread']['name'] for p in h.posts}=={'spaces/QA0/threads/T','spaces/QA0/threads/T2'}
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_restart_keeps_room_and_card_but_does_not_repost_prior_answer(native_context,signing,credentials):
    from tests.test_channel_connection_telegram_runtime import buttons
    async def run():
        from app.models import ChannelConnectionDelivery
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;buttons(c);a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step();first=await handshake(a,adapter,header,event());await finish(a);packet=h.posts[0][1]
            with c.sessions() as db:room=db.get(ChannelConnectionDelivery,UUID(first['delivery_id'])).room_id
            await a.stop();a=ConnectionSupervisor(c.sessions,{'google_chat':adapter},owner=c.owner);await a.step();await finish(a);assert len(h.posts)==1
            second=await adapter.receive(header,click(packet));await finish(a);assert len(h.posts)==2
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(second['delivery_id'])).room_id==room
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_disconnect_after_first_post_stops_second_and_preserves_partial_unknown(native_context,signing,credentials,monkeypatch):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        import app.services.channel_connection_supervisor as supervisor
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);convert=supervisor.convert_channel_packet;send=h.send
        def doubled(provider,mode,payload):
            packets=convert(provider,mode,payload);return packets+packets if provider=='google-chat' and mode=='replies' else packets
        def disconnecting(*args):
            result=send(*args)
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            return result
        monkeypatch.setattr(supervisor,'convert_channel_packet',doubled);h.send=disconnecting
        try:
            await a.step();first=await handshake(a,adapter,header,event());await finish(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(first['delivery_id'])).status=='unknown'
            assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_changed_duplicate_message_is_conflict_without_reexecution(native_context,signing,credentials):
    async def run():
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step();body=event();await handshake(a,adapter,header,body);await finish(a);body['message']['text']='@CGA changed question'
            with pytest.raises(HTTPException,match='NATIVE_EVENT_CONFLICT'):await adapter.receive(header,body)
            await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
@pytest.mark.parametrize('project',['1234567890','9876543210'])
def test_same_project_cannot_be_claimed_by_another_organization(native_context,signing,credentials,project):
    async def run():
        from app.models import Organization,Group,Bot,AdminChannel,ChannelConnection,ChannelInstallationLease
        from app.services.channel_connection_supervisor import ConnectionSpec
        from app.services.channel_connection_google_chat import GoogleChatAdapter
        from uuid import uuid4
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);ids=None
        try:
            await a.step()
            with c.sessions() as db:
                suffix=uuid4().hex;org=Organization(code='c07-other-'+suffix,name='C07 other QA');db.add(org);db.flush()
                group=Group(organization_id=org.id,code='QA',name='C07 other');db.add(group);db.flush()
                bot=Bot(organization_id=org.id,group_id=group.id,name='C07 other QA');parent=AdminChannel(organization_id=org.id,code='GOOGLE_CHAT',name='C07 other',data_json={'provider':'google_chat','renderer_type':'google_chat'});db.add_all([bot,parent]);db.flush()
                row=ChannelConnection(organization_id=org.id,channel_id=parent.id,bot_id=bot.id,name='C07 other',provider='google_chat',installation_id=project,account_id='users/100',target_id='spaces/OTHER',desired_state='connected',observed_state='connecting');db.add(row);db.commit()
                ids=(org.id,group.id,bot.id,parent.id,row.id);spec=ConnectionSpec(row.id,org.id,'google_chat',row.generation,row.installation_id,row.account_id,row.target_id)
            other=GoogleChatAdapter(c.sessions,owner='c07-other-worker',http=Http(),authenticator=adapter.authenticator)
            with pytest.raises(HTTPException,match='GOOGLE_CHAT_APP_SCOPE_IN_USE'):await other.verify(spec,credentials)
            assert not h.posts
        finally:
            await a.stop()
            if ids:
                with c.sessions() as db:
                    oid,gid,bid,pid,cid=ids
                    for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==oid)):db.delete(lease)
                    db.flush()
                    for model,identifier in [(ChannelConnection,cid),(AdminChannel,pid),(Bot,bid),(Group,gid),(Organization,oid)]:db.delete(db.get(model,identifier));db.flush()
                    db.commit()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_real_asgi_route_signature_commit_and_existing_qa_round_trip(native_context,signing,credentials,monkeypatch):
    async def run():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import channel_connection_events as route
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        monkeypatch.setattr(route.settings,'channel_connection_worker_enabled',True);monkeypatch.setitem(route.ADAPTERS,'google_chat',adapter)
        app=FastAPI();app.include_router(route.router,prefix='/api/v1');client=TestClient(app);url='/api/v1/channel-connections/google-chat/events'
        try:
            await a.step();body=event()
            anonymous=await asyncio.to_thread(client.post,url,json=body,headers={'X-Aidot-Api-Key':'synthetic-shared-key'})
            assert anonymous.status_code==401 and not h.posts
            request=asyncio.create_task(asyncio.to_thread(client.post,url,json=body,headers={'Authorization':header}))
            for _ in range(60):
                await asyncio.sleep(.03);await a.step()
                if request.done():break
            response=await request;assert response.status_code==200 and response.json()=={}
            await finish(a);assert len(h.posts)==1
            repeated=await asyncio.to_thread(client.post,url,json=body,headers={'Authorization':header});assert repeated.status_code==200
            await finish(a);assert len(h.posts)==1
        finally:client.close();await a.stop()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_verification_delay_does_not_discard_first_question_after_connect_intent(native_context,signing,credentials):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);get=h.get;available=[False]
        def delayed(*args):
            if not available[0]:raise HTTPException(503,'SYNTHETIC_MEMBERSHIP_NOT_READY')
            return get(*args)
        h.get=delayed
        try:
            await a.step();body=event();await asyncio.sleep(.02);available[0]=True;await a.step()
            first=await handshake(a,adapter,header,body);await finish(a)
            with c.sessions() as db:item=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert item.status=='sent',item.last_error_code
            assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_message_before_connect_intent_is_not_executed(native_context,signing,credentials):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials)
        try:
            await a.step();body=event()
            with c.sessions() as db:body['eventTime']=(db.get(ChannelConnection,c.ids['connections'][0]).accept_after-timedelta(seconds=1)).isoformat()
            first=await handshake(a,adapter,header,body);await finish(a)
            with c.sessions() as db:item=db.get(ChannelConnectionDelivery,UUID(first['delivery_id']));assert item.status=='cancelled' and item.last_error_code=='BEFORE_CONNECTION'
            assert h.posts==[]
        finally:await a.stop()
    asyncio.run(run())



@pytest.mark.parametrize('native_context',['google_chat'],indirect=True)
def test_direct_message_uses_private_room_without_named_space_reply_option(native_context,signing,credentials):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,header=setup(c,signing,credentials);get=h.get
        def direct(path,token):
            value=get(path,token)
            if path=='spaces/QA1':value['spaceType']='DIRECT_MESSAGE'
            return value
        h.get=direct
        try:
            await a.step();shared=await handshake(a,adapter,header,event());await finish(a)
            body=event(1,actor='users/501',addressed=False);body['space']['spaceType']='DIRECT_MESSAGE'
            private=await handshake(a,adapter,header,body);await finish(a)
            assert len(h.posts)==2 and h.posts[-1][0]=='spaces/QA1' and h.posts[-1][3] is None
            with c.sessions() as db:
                one=db.get(ChannelConnectionDelivery,UUID(shared['delivery_id']));two=db.get(ChannelConnectionDelivery,UUID(private['delivery_id']));assert one.room_id!=two.room_id and two.event_json['roomMode']=='private'
        finally:await a.stop()
    asyncio.run(run())
