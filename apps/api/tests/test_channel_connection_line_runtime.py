import asyncio,json,time,base64,hmac,hashlib
from datetime import timedelta
from uuid import UUID
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_supervisor import drain
from tests.test_channel_connection_teams_adapter import handshake

BOT='U'+'1'*32;SECRET='synthetic-line-secret'
def source(index=0):return 'C'+format(index+2,'032x')
def raw(body):return json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
def signature(body):return base64.b64encode(hmac.new(SECRET.encode(),body,hashlib.sha256).digest()).decode()
def event(index=0,identifier='m1',actor=None):
    return {'destination':BOT,'events':[{'type':'message','mode':'active','timestamp':int(time.time()*1000),'webhookEventId':identifier,'replyToken':'synthetic-reply-'+identifier,'source':{'type':'group','groupId':source(index),'userId':actor or 'U'+'5'*32},'message':{'id':identifier,'type':'text','text':'@CGA 검증 질문','mention':{'mentionees':[{'type':'user','userId':BOT,'isSelf':True,'index':0,'length':5}]}},'deliveryContext':{'isRedelivery':False}}]}
class Http:
    def __init__(self):self.posts=[];self.fail=False
    def bot_info(self,token):return {'userId':BOT}
    def reply(self,token,body):
        self.posts.append(body)
        if self.fail:raise TimeoutError('synthetic uncertain reply')
        return {'sentMessages':[{'id':'out'+str(len(self.posts))}]}
def setup(c):
    from app.models import ChannelConnection,ChannelInstallationLease
    from app.core.config import settings
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.services.channel_connection_line import LineAdapter
    with c.sessions() as db:
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id=BOT;row.account_id=BOT;row.target_id=source(i);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)-timedelta(seconds=1)
            row.credential_ciphertext=encrypt_credentials({'access_token':'synthetic-only','channel_secret':SECRET},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    h=Http();adapter=LineAdapter(c.sessions,owner=c.owner,http=h);return ConnectionSupervisor(c.sessions,{'line':adapter},owner=c.owner),adapter,h
async def receive(adapter,body):
    data=raw(body);return await adapter.receive(signature(data),data)
async def first(a,adapter,body):
    # 테스트의 작업자가 실제 서명 요청과 함께 최초 증명을 활성화한다.
    task=asyncio.create_task(receive(adapter,body))
    for _ in range(60):
        await asyncio.sleep(.02);await a.step()
        if task.done():return await task
    return await task
async def finish(a):await a.step();await drain(a);await a.step();await drain(a)

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_signed_webhook_uses_existing_room_and_replies_once_with_protected_token(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();body=event();result=await first(a,adapter,body);assert await receive(adapter,body)==result;await finish(a)
            assert len(h.posts)==1 and h.posts[0]['replyToken']=='synthetic-reply-m1' and '정의한 로컬 답변' in str(h.posts[0]['messages'])
            with c.sessions() as db:
                item=db.get(ChannelConnectionDelivery,UUID(result['deliveries'][0]));assert item.status=='sent' and item.room_id and item.queue_event_id and 'synthetic-reply' not in json.dumps(item.event_json) and 'synthetic-reply' not in item.context_ciphertext
            body['events'][0]['deliveryContext']['isRedelivery']=True;assert await receive(adapter,body)==result;await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_bad_signature_never_commits_or_executes(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step()
            with pytest.raises(HTTPException) as err:await adapter.receive(signature(raw(event())),raw(event(identifier='tampered')))
            assert err.value.status_code==401
            with c.sessions() as db:assert db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id.in_(c.ids['connections']))) is None
            assert not h.posts
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_reply_timeout_is_unknown_and_redelivery_never_executes_or_replies_again(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);h.fail=True
        try:
            await a.step();body=event();result=await first(a,adapter,body);await finish(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(result['deliveries'][0])).status=='unknown'
            body['events'][0]['deliveryContext']['isRedelivery']=True;assert await receive(adapter,body)==result;await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['line'],indirect=True)
@pytest.mark.parametrize('phase',['before-execution','before-reply'])
def test_expired_reply_never_executes_or_posts_again(native_context,phase):
    async def run():
        from app.models import ChannelConnectionDelivery
        from app.core.config import settings
        from app.services.channel_connections import decrypt_connection_state,encrypt_connection_state
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await first(a,adapter,event());await finish(a);assert len(h.posts)==1
            accepted=await receive(adapter,event(identifier='expired'));did=UUID(accepted['deliveries'][0])
            if phase=='before-reply':await a.step();await drain(a)
            with c.sessions() as db:
                row=db.get(ChannelConnectionDelivery,did);binding=decrypt_connection_state(row.context_ciphertext,c.ids['org_id'],row.connection_id,settings.channel_connection_encryption_key,row.context_key_version,'delivery:'+str(did));binding['received_at']-=56
                row.context_ciphertext=encrypt_connection_state(binding,c.ids['org_id'],row.connection_id,settings.channel_connection_encryption_key,row.context_key_version,'delivery:'+str(did));db.commit()
            await finish(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,did).status=='failed'
            assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_changed_reply_proof_duplicate_is_rejected_without_reexecution(native_context):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();body=event();await first(a,adapter,body);await finish(a)
            body['events'][0]['replyToken']='different-synthetic-reply'
            with pytest.raises(HTTPException) as err:await receive(adapter,body)
            assert err.value.status_code==409;await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_shared_room_turn_blocks_second_actor_but_other_target_runs(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await first(a,adapter,event());await finish(a)
            pending=await receive(adapter,event(identifier='pending'))
            busy=await receive(adapter,event(identifier='busy',actor='U'+'6'*32))
            with c.sessions() as db:row=db.get(ChannelConnectionDelivery,UUID(busy['deliveries'][0]));assert row.status=='failed' and row.last_error_code=='ROOM_BUSY'
            await first(a,adapter,event(1,identifier='independent'));await finish(a)
            assert len(h.posts)==3
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_valid_empty_probe_does_not_activate_and_bad_json_is_rejected(native_context):
    async def run():
        from app.models import ChannelConnection
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();assert await receive(adapter,{'destination':BOT,'events':[]})=={'deliveries':[]}
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).observed_state=='connecting'
            with pytest.raises(HTTPException) as err:await adapter.receive(signature(b'{'),b'{')
            assert err.value.status_code==422 and not h.posts
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_restart_and_transient_identity_error_preserve_original_accept_cutoff(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h=setup(c);info=h.bot_info
        try:
            await a.step();await first(a,adapter,event());await finish(a)
            with c.sessions() as db:cutoff=db.get(ChannelConnection,c.ids['connections'][0]).accept_after
            await a.stop();a=ConnectionSupervisor(c.sessions,{'line':adapter},owner=c.owner)
            def fail(*_):raise HTTPException(503,'SYNTHETIC_IDENTITY_UNAVAILABLE')
            h.bot_info=fail;await a.step();body=event(identifier='during-error');await asyncio.sleep(.02);h.bot_info=info;await a.step()
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).accept_after==cutoff
            accepted=await receive(adapter,body);await finish(a)
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(accepted['deliveries'][0])).status=='sent'
            assert len(h.posts)==2
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_malformed_source_is_rejected_after_signature_without_execution(native_context):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();body=event();body['events'][0]['source']='not-an-object'
            with pytest.raises(HTTPException) as err:await receive(adapter,body)
            assert err.value.status_code==422 and not h.posts
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_batch_partial_receipt_recovery_does_not_reexecute_committed_question(native_context):
    async def run():
        from copy import deepcopy
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await first(a,adapter,event());await finish(a)
            batch=event(identifier='batch1');second=event(identifier='batch2')['events'][0];second['source']['groupId']=source(9);batch['events'].append(second)
            with pytest.raises(HTTPException) as err:await receive(adapter,batch)
            assert err.value.status_code==403;await finish(a);assert len(h.posts)==2
            batch['events'][1]['source']['groupId']=source();result=await receive(adapter,batch);assert len(result['deliveries'])==2;await finish(a);assert len(h.posts)==3
            await receive(adapter,batch);await finish(a);assert len(h.posts)==3
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_general_chat_and_missing_actor_never_create_questions(native_context):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();body=event();body['events'][0]['message'].pop('mention');body['events'][0]['message']['text']='그냥 대화';assert await receive(adapter,body)=={'deliveries':[]}
            body=event();body['events'][0]['source'].pop('userId');assert await receive(adapter,body)=={'deliveries':[]};assert not h.posts
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_private_source_has_private_room_and_independent_target(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);actor='U'+'5'*32
        with c.sessions() as db:db.get(ChannelConnection,c.ids['connections'][0]).target_id=actor;db.commit()
        try:
            await a.step();body=event();native=body['events'][0];native['source']={'type':'user','userId':actor};native['message']['text']='검증 질문';native['message'].pop('mention')
            accepted=await first(a,adapter,body);await finish(a)
            with c.sessions() as db:item=db.get(ChannelConnectionDelivery,UUID(accepted['deliveries'][0]));assert item.status=='sent' and item.event_json['roomMode']=='private'
            assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_card_actor_source_and_redelivery_restore_existing_variable(native_context):
    from tests.test_channel_connection_telegram_runtime import buttons
    async def run():
        from copy import deepcopy
        from uuid import uuid4
        c=native_context;buttons(c);a,adapter,h=setup(c)
        try:
            await a.step();await first(a,adapter,event());await finish(a);packet=h.posts[0]['messages'][0]
            data=packet['contents']['footer']['contents'][1]['action']['data']
            def click(identifier,data=data,actor='U'+'5'*32):
                body=event(identifier=identifier,actor=actor);value=body['events'][0];value['type']='postback';value.pop('message');value['postback']={'data':data};return body
            for body in [click('wrong-source','cga:'+str(uuid4())+':1'),click('wrong-actor',actor='U'+'6'*32)]:await receive(adapter,body);await finish(a)
            assert len(h.posts)==1
            body=click('valid-click');await receive(adapter,body);await finish(a);assert len(h.posts)==2 and '선택: 다음' in str(h.posts[-1]['messages'])
            await receive(adapter,body);await finish(a);assert len(h.posts)==2
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_duplicate_acceptance_does_not_wait_for_result_delivery_lock(native_context,monkeypatch):
    async def run():
        import threading
        from app.services import channel_connection_state as state
        c=native_context;a,adapter,h=setup(c);held=threading.Event();release=threading.Event();original=state.mark_ready
        def holding(*args,**kwargs):
            value=original(*args,**kwargs);held.set()
            if not release.wait(5):raise AssertionError('controlled result lock deadline')
            return value
        monkeypatch.setattr(state,'mark_ready',holding)
        try:
            await a.step();body=event();result=await first(a,adapter,body)
            await a.step();assert await asyncio.to_thread(held.wait,3)
            try:assert await asyncio.wait_for(receive(adapter,body),.5)==result
            finally:release.set()
            await finish(a);assert len(h.posts)==1
        finally:release.set();await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['line'],indirect=True)
@pytest.mark.parametrize('index',[0,1])
def test_same_reply_token_cannot_fund_another_event_or_target(native_context,index):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();original=event();await first(a,adapter,original);await finish(a)
            another=event(index,identifier='same-token-other-event');another['events'][0]['replyToken']=original['events'][0]['replyToken']
            with pytest.raises(HTTPException) as err:
                if index==0:await receive(adapter,another)
                else:await first(a,adapter,another)
            assert err.value.status_code==409;await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_authenticated_bot_cannot_be_claimed_by_another_organization(native_context):
    async def run():
        from app.models import Organization,Group,Bot,AdminChannel,ChannelConnection,ChannelInstallationLease
        from app.services.channel_connection_supervisor import ConnectionSpec
        from app.services.channel_connection_line import LineAdapter
        from uuid import uuid4
        c=native_context;a,adapter,h=setup(c);ids=None
        try:
            await a.step()
            with c.sessions() as db:
                suffix=uuid4().hex;org=Organization(code='c08-other-'+suffix,name='C08 other QA');db.add(org);db.flush()
                group=Group(organization_id=org.id,code='QA',name='C08 other');db.add(group);db.flush()
                bot=Bot(organization_id=org.id,group_id=group.id,name='C08 other QA');parent=AdminChannel(organization_id=org.id,code='LINE',name='C08 other',data_json={'provider':'line','renderer_type':'line'});db.add_all([bot,parent]);db.flush()
                row=ChannelConnection(organization_id=org.id,channel_id=parent.id,bot_id=bot.id,name='C08 other',provider='line',installation_id=BOT,account_id=BOT,target_id=source(9),desired_state='connected',observed_state='connecting');db.add(row);db.commit();ids=(org.id,group.id,bot.id,parent.id,row.id)
                spec=ConnectionSpec(row.id,org.id,'line',row.generation,BOT,BOT,source(9))
            other=LineAdapter(c.sessions,owner='c08-other',http=h)
            with pytest.raises(HTTPException,match='LINE_APP_SCOPE_IN_USE'):await other.verify(spec,{'access_token':'synthetic-only','channel_secret':SECRET})
            assert not h.posts
        finally:
            await a.stop()
            if ids:
                with c.sessions() as db:
                    oid,gid,bid,pid,cid=ids
                    for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==oid)):db.delete(lease)
                    for model,identifier in [(ChannelConnection,cid),(AdminChannel,pid),(Bot,bid),(Group,gid),(Organization,oid)]:db.delete(db.get(model,identifier))
                    db.commit()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_signed_other_destination_does_not_consume_this_bot(native_context):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();body=event();body['destination']='U'+'9'*32
            with pytest.raises(HTTPException) as err:await receive(adapter,body)
            assert err.value.status_code==403 and not h.posts
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['line'],indirect=True)
def test_deactivated_target_before_reply_never_posts(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await first(a,adapter,event());await finish(a)
            answer=await receive(adapter,event(identifier='disconnect-before-post'));await a.step();await drain(a)
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await finish(a);assert len(h.posts)==1
            with c.sessions() as db:assert db.get(ChannelConnectionDelivery,UUID(answer['deliveries'][0])).status!='sent'
        finally:await a.stop()
    asyncio.run(run())
