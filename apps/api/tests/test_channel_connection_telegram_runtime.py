import asyncio,time,queue,json,threading
from datetime import timedelta
from sqlalchemy import select
import pytest
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_supervisor import drain
from tests.test_channel_connection_telegram import Http


class NativeHttp(Http):
    def __init__(self):super().__init__();self.queue=queue.Queue();self.polls=[];self.posts=[];self.acks=[];self.fail=False;self.fail_ack=False;self.pending={};self.lock=threading.Lock()
    def call(self,method,token,payload):
        if method=='getUpdates':
            self.polls.append(dict(payload))
            try:
                update=self.queue.get(timeout=.1 if payload.get('timeout') else 0)
                with self.lock:self.pending[update['update_id']]=update
            except queue.Empty:pass
            with self.lock:
                self.pending={k:v for k,v in self.pending.items() if k>=payload.get('offset',0)}
                return [v for k,v in sorted(self.pending.items())][:payload.get('limit',100)]
        if method=='getChat':return {'id':int(payload['chat_id']),'type':'supergroup','permissions':{'can_send_messages':True,'can_send_photos':True}}
        if method in {'sendMessage','sendPhoto'}:
            self.posts.append((method,payload))
            if self.fail:raise TimeoutError('synthetic uncertain POST')
            result={'message_id':len(self.posts),'chat':{'id':int(payload['chat_id'])}}
            if 'message_thread_id' in payload:result['message_thread_id']=payload['message_thread_id']
            return result
        if method=='answerCallbackQuery':
            self.acks.append(payload['callback_query_id'])
            if self.fail_ack:raise TimeoutError('synthetic uncertain callback')
            return True
        return super().call(method,token,payload)


def message(update_id=1,chat=-300,actor=500,*,addressed=True,thread=None):
    text=('@CgaQaBot ' if addressed else '')+'검증 질문';body={'message_id':update_id,'date':int(time.time()),'chat':{'id':chat,'type':'supergroup'},'from':{'id':actor,'is_bot':False},'text':text,'entities':[{'type':'mention','offset':0,'length':9}] if addressed else []}
    if thread is not None:body.update(message_thread_id=thread,is_topic_message=True)
    return {'update_id':update_id,'message':body}


def setup(c):
    from app.models import ChannelConnection,ChannelInstallationLease
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.services.channel_connection_telegram import TelegramAdapter
    from app.core.config import settings
    h=NativeHttp()
    with c.sessions() as db:
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id='100';row.account_id='100';row.target_id=str(-300-i);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)
            row.credential_ciphertext=encrypt_credentials({'bot_token':'synthetic-only'},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    adapter=TelegramAdapter(c.sessions,owner=c.owner,http=h);return ConnectionSupervisor(c.sessions,{'telegram':adapter},owner=c.owner),adapter,h


async def delivered(a,h,update):
    h.queue.put(update)
    for _ in range(150):
        run=next(iter(a.runs.values()))
        if run.task.done():run.task.result()
        if run.listener.offset>update['update_id']:return
        await asyncio.sleep(.02)
    raise AssertionError('committed Telegram offset missing')


async def finish(a):await a.step();await drain(a);await a.step();await drain(a)


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_poll_to_existing_fixed_qa_then_native_post_is_deduplicated(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();assert len(a.runs)==1
            update=message();await delivered(a,h,update);await finish(a)
            assert len(h.posts)==1 and h.posts[0][1]['chat_id']=='-300' and '정의한 로컬 답변' in h.posts[0][1]['text']
            await delivered(a,h,update);await finish(a);assert len(h.posts)==1
            with c.sessions() as db:
                row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]));assert row.status=='sent' and row.room_id and row.queue_event_id
            assert any(p.get('offset')==2 for p in h.polls)
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_busy_question_is_not_delayed_and_other_chat_continues_after_disconnect(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await delivered(a,h,message());await delivered(a,h,message(2,actor=501));await finish(a);assert len(h.posts)==1
            with c.sessions() as db:
                assert db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.last_error_code=='ROOM_BUSY'))
                disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step();await delivered(a,h,message(3,chat=-301));await finish(a);assert len(h.posts)==2 and h.posts[-1][1]['chat_id']=='-301' and len(a.runs)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_post_timeout_is_unknown_and_never_retried(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);h.fail=True
        try:
            await a.step();await delivered(a,h,message());await finish(a)
            for _ in range(3):await finish(a)
            assert len(h.posts)==1
            with c.sessions() as db:assert db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])).status=='unknown'
        finally:await a.stop()
    asyncio.run(run())


def buttons(c):
    from copy import deepcopy
    from app.models import BotVersion
    with c.sessions() as db:
        version=db.get(BotVersion,c.ids['version_id']);doc=deepcopy(version.version_json);graph=doc['dialog_flow_graphs'][1];talk=next(n for n in graph['nodes'] if n['kind']=='talk')
        talk['config']={'messageType':'button','basicMessages':['선택하세요'],'messages':['확인','다음'],'responseType':'relay','responseVariableName':'choice'}
        graph['nodes'].append({'id':'telegram-result','kind':'talk','config':{'messageType':'text','basicMessages':['선택: {{$choice}}'],'responseType':'none'}})
        for link in graph['links']:
            if link['sourceNodeId']==talk['id']:link['targetNodeId']='telegram-result'
        graph['links'].append({'sourceNodeId':'telegram-result','sourcePort':'next','targetNodeId':'local-end'});version.version_json=doc;db.commit()


def action(update_id,data,stamp,actor=500):return {'update_id':update_id,'callback_query':{'id':'synthetic-callback-'+str(update_id),'from':{'id':actor,'is_bot':False},'message':{'message_id':900,'date':stamp,'from':{'id':100,'is_bot':True},'chat':{'id':-300,'type':'supergroup'}},'data':data}}


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
@pytest.mark.parametrize('unknown',[False,True])
def test_button_receipt_is_committed_before_execution_and_unknown_never_runs(native_context,unknown):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);buttons(c);observed=[];original=h.call
        def call(method,token,payload):
            if method=='answerCallbackQuery':
                run=next(iter(a.runs.values()))
                with c.sessions() as db:
                    row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.event_json['eventId'].astext=='telegram-update:2'))
                    observed.append(row.status=='received' and row.execution_claim_id is None and run.listener.gate.context(row)['telegram_ack']['status']=='sending')
                assert a._claim_work(run,next(iter(run.targets.values()))) is None
            return original(method,token,payload)
        h.call=call;h.fail_ack=unknown
        try:
            await a.step();await delivered(a,h,message());await finish(a);stamp=int(time.time());data=h.posts[0][1]['reply_markup']['inline_keyboard'][1][0]['callback_data']
            frame=action(2,data,stamp);await delivered(a,h,frame);await finish(a)
            assert observed==[True] and len(h.acks)==1 and len(h.posts)==(1 if unknown else 2)
            if not unknown:assert '선택: 다음' in h.posts[-1][1]['text']
            await delivered(a,h,frame);await finish(a);assert len(h.acks)==1
            with c.sessions() as db:
                row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.event_json['eventId'].astext=='telegram-update:2'));assert row.status==('unknown' if unknown else 'sent') and 'callback_id' not in next(iter(a.runs.values())).listener.gate.context(row)
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_topic_rooms_and_general_chat_bot_identity_are_preserved(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await delivered(a,h,message(addressed=False));await finish(a);assert not h.posts
            one=message(2,thread=10);one['message']['from']['is_bot']=True;await delivered(a,h,one);await finish(a)
            await delivered(a,h,message(3,thread=11,actor=501));await finish(a);assert len(h.posts)==2 and {p[1]['message_thread_id'] for p in h.posts}=={10,11}
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.event_json['addressed'].astext=='true')));assert len(rows)==2 and rows[0].room_id!=rows[1].room_id and any(x.event_json['participantKind']=='bot' for x in rows)
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_automatic_restart_keeps_original_card_and_offset(native_context):
    async def run():
        from app.services.channel_connection_telegram import TelegramAdapter
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        from app.models import ChannelConnection
        c=native_context;a,adapter,h=setup(c);buttons(c);b=None
        try:
            await a.step();await delivered(a,h,message());await finish(a);stamp=int(time.time());data=h.posts[0][1]['reply_markup']['inline_keyboard'][1][0]['callback_data']
            with c.sessions() as db:cutoff=db.get(ChannelConnection,c.ids['connections'][0]).accept_after
            await a.stop();other=TelegramAdapter(c.sessions,owner='c06-restarted',http=h);b=ConnectionSupervisor(c.sessions,{'telegram':other},owner='c06-restarted');await b.step()
            await delivered(b,h,action(2,data,stamp));await finish(b);assert len(h.posts)==2 and '선택: 다음' in h.posts[-1][1]['text']
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).accept_after==cutoff
        finally:
            if not a.stopping:await a.stop()
            if b:await b.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_manual_reconnect_does_not_execute_previous_card(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h=setup(c);buttons(c)
        try:
            await a.step();await delivered(a,h,message());await finish(a);stamp=int(time.time());data=h.posts[0][1]['reply_markup']['inline_keyboard'][1][0]['callback_data']
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step()
            with c.sessions() as db:
                row=db.get(ChannelConnection,c.ids['connections'][0]);row.generation+=1;row.desired_state='connected';row.observed_state='connecting';row.lease_owner=None;row.accept_after=__import__('app.services.channel_connection_state',fromlist=['clock']).clock(db);db.commit()
            await a.step();await delivered(a,h,action(2,data,stamp));await finish(a);assert len(h.posts)==1 and not h.acks
            with c.sessions() as db:
                row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.event_json['eventId'].astext=='telegram-update:2'));assert row.status=='cancelled' and row.last_error_code=='BEFORE_CONNECTION'
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_database_commit_failure_does_not_ack_offset_and_invalid_packet_is_isolated(native_context):
    async def run():
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();listener=next(iter(a.runs.values())).listener
            async def reject(_):raise RuntimeError('synthetic DB commit failure')
            with pytest.raises(RuntimeError):await listener.handle(message(99),reject)
            assert listener.gate.load_cursor()['offset']==0 and not h.posts
            invalid=message();invalid['message'].pop('text');invalid['message']['photo']=[{}];await delivered(a,h,invalid)
            await delivered(a,h,message(2,chat=-301));await finish(a);assert len(h.posts)==1 and len(a.runs)==1 and h.posts[0][1]['chat_id']=='-301'
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_second_worker_cannot_poll_the_same_authenticated_bot(native_context):
    async def run():
        from app.services.channel_connection_telegram import TelegramAdapter
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h=setup(c);other_http=NativeHttp();b=ConnectionSupervisor(c.sessions,{'telegram':TelegramAdapter(c.sessions,owner='c06-other',http=other_http)},owner='c06-other')
        try:await a.step();await b.step();assert len(a.runs)==1 and not b.runs and not other_http.polls
        finally:await b.stop();await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_native_photo_packet_permission_and_destination_preflight(native_context):
    async def run():
        from dataclasses import replace
        from app.services.channel_connection_supervisor import NativeNotSubmitted
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await delivered(a,h,message());await a.step();await drain(a)
            run=next(iter(a.runs.values()));target=run.targets[c.ids['connections'][0]]
            from app.models import ChannelConnectionDelivery
            with c.sessions() as db:did=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==target.spec.id))
            claim,_,context=a._send_data(run,target,did)
            packet={'method':'sendPhoto','parameters':{'chat_id':'-300','photo':'https://example.invalid/qa.png','caption':'QA'}}
            denied=replace(target,transport=replace(target.transport,can_send_photos=False))
            with pytest.raises(NativeNotSubmitted):await run.listener.send(denied,did,[packet],context)
            with pytest.raises(NativeNotSubmitted):await run.listener.send(target,did,[{'method':'sendPhoto','parameters':{**packet['parameters'],'chat_id':'-301'}}],context)
            assert not h.posts
            assert await run.listener.send(target,did,[packet],context)==['-300:1'] and h.posts[0][0]=='sendPhoto'
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_partial_native_post_then_disconnect_is_unknown_and_not_reissued(native_context):
    async def run():
        from copy import deepcopy
        from app.models import BotVersion,ChannelConnection,ChannelConnectionDelivery
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h=setup(c);original=h.call
        with c.sessions() as db:
            version=db.get(BotVersion,c.ids['version_id']);doc=deepcopy(version.version_json);graph=doc['dialog_flow_graphs'][1];talk=next(n for n in graph['nodes'] if n['kind']=='talk')
            graph['nodes'].append({'id':'telegram-second','kind':'talk','config':{'messageType':'text','basicMessages':['두 번째 답변'],'responseType':'none'}})
            for link in graph['links']:
                if link['sourceNodeId']==talk['id']:link['targetNodeId']='telegram-second'
            graph['links'].append({'sourceNodeId':'telegram-second','sourcePort':'next','targetNodeId':'local-end'});version.version_json=doc;db.commit()
        def call(method,token,payload):
            result=original(method,token,payload)
            if method=='sendMessage' and len(h.posts)==1:
                with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            return result
        h.call=call
        try:
            await a.step();await delivered(a,h,message());await finish(a);assert len(h.posts)==1
            with c.sessions() as db:assert db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])).status=='unknown'
            await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_button_source_and_participant_are_checked_before_cga_core(native_context):
    async def run():
        from uuid import uuid4
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);buttons(c)
        try:
            await a.step();await delivered(a,h,message());await finish(a);stamp=int(time.time());data=h.posts[0][1]['reply_markup']['inline_keyboard'][1][0]['callback_data']
            for frame in [action(2,'cga:'+str(uuid4())+':1',stamp),action(3,data,stamp,actor=501)]:
                await delivered(a,h,frame);await finish(a)
            assert len(h.posts)==1
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.event_json['eventId'].astext.in_(['telegram-update:2','telegram-update:3']))));assert len(rows)==2 and all(x.status=='failed' and x.queue_event_id is None for x in rows)
            await delivered(a,h,action(4,data,stamp));await finish(a);assert len(h.posts)==2 and '선택: 다음' in h.posts[-1][1]['text']
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_initial_pending_message_is_not_executed_before_connection_cutoff(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);h.queue.put(message())
        try:
            await a.step();listener=next(iter(a.runs.values())).listener
            for _ in range(150):
                if listener.offset>1:break
                await asyncio.sleep(.02)
            await finish(a);assert not h.posts and listener.offset==2
            with c.sessions() as db:assert db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])).status=='cancelled'
            await delivered(a,h,message(2));await finish(a);assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_private_chat_and_group_keep_separate_existing_rooms(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelConnectionDelivery
        c=native_context;a,adapter,h=setup(c);original=h.call
        with c.sessions() as db:db.get(ChannelConnection,c.ids['connections'][1]).target_id='300';db.commit()
        def call(method,token,payload):
            if method=='getChat' and payload['chat_id']=='300':return {'id':300,'type':'private'}
            return original(method,token,payload)
        h.call=call
        try:
            await a.step();await delivered(a,h,message());await finish(a)
            frame=message(2,chat=300,actor=300,addressed=False);frame['message']['chat']['type']='private';await delivered(a,h,frame);await finish(a)
            assert len(h.posts)==2 and h.posts[-1][1]['chat_id']=='300'
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id.in_(c.ids['connections']))));assert len(rows)==2 and rows[0].room_id!=rows[1].room_id and {x.event_json['roomMode'] for x in rows}=={'private','shared'}
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['telegram'],indirect=True)
def test_long_idle_lower_id_restarts_with_epoch_and_preserves_unknown(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery,ChannelInstallationLease,AuditLog
        from app.services import channel_connection_state as state
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h=setup(c)
        try:
            await a.step();await delivered(a,h,message(1));await finish(a)
            h.fail=True;await delivered(a,h,message(2,chat=-301));await finish(a)
            listener=next(iter(a.runs.values())).listener;gate=listener.gate
            assert len(h.posts)==2
            with c.sessions() as db:
                old=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][1]));old_id=old.id;assert old.status=='unknown'
                lease=db.get(ChannelInstallationLease,gate.lease_id);v=gate._load(lease);v['last_commit_at']=(state.clock(db)-timedelta(days=8)).isoformat();gate._save(lease,v);db.commit()
            await a.stop();h.fail=False
            with h.lock:h.pending.clear()
            before=len(h.polls)
            a=ConnectionSupervisor(c.sessions,{'telegram':adapter},owner=c.owner);await a.step()
            assert h.polls[before]['offset']==0
            await delivered(a,h,message(1));await finish(a)
            with c.sessions() as db:diagnostic=[(x.status,x.last_error_code,x.event_json['eventId']) for x in db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]))]
            assert len(h.posts)==3,diagnostic
            await delivered(a,h,message(1));await finish(a);assert len(h.posts)==3
            await a.stop();a=ConnectionSupervisor(c.sessions,{'telegram':adapter},owner=c.owner);await a.step()
            assert next(iter(a.runs.values())).listener.gate.load_cursor()['epoch']==1
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])))
                assert len(rows)==2 and db.get(ChannelConnectionDelivery,old_id).status=='unknown'
                assert {x.event_json['eventId'] for x in rows}=={'telegram-update:1','telegram-epoch:1:telegram-update:1'}
                for audit in db.scalars(select(AuditLog).where(AuditLog.target_id==gate.lease_id,AuditLog.action_type=='system.connection.telegram_idle_reset')):db.delete(audit)
                db.commit()
        finally:await a.stop()
    asyncio.run(run())
