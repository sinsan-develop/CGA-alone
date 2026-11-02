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
