import asyncio,json,time
from uuid import UUID
from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_supervisor import drain
from tests.test_channel_connection_slack import subject,Http,Ws,Socket,socket_frame


class NativeHttp(Http):
    def __init__(self):super().__init__();self.posts=[];self.fail=False;self.responses['apps.connections.open']={'ok':True,'url':'wss://wss.slack.com/link/?ticket=synthetic-only'}
    def call(self,method,token,payload):
        if method=='conversations.info':return {'ok':True,'channel':{'id':payload['channel'],'is_member':True,'is_archived':False}}
        if method=='chat.postMessage':
            self.posts.append(payload)
            if self.fail:raise TimeoutError('synthetic submission unknown')
            return {'ok':True,'channel':payload['channel'],'ts':f'{time.time():.6f}'}
        return super().call(method,token,payload)


def setup(c):
    from app.models import ChannelConnection,ChannelInstallationLease,AdminChannel
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.core.config import settings
    h=NativeHttp();ws=Ws();sock=Socket(ws)
    with c.sessions() as db:
        db.get(AdminChannel,c.ids['parent_id']).code='SLACK'
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id='TQA001';row.account_id='UQA001';row.target_id='CQA00'+str(i+1);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)-timedelta(seconds=1)
            row.credential_ciphertext=encrypt_credentials({'bot_token':'synthetic-bot-token','app_token':'synthetic-app-token'},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    adapter=subject().SlackAdapter(c.sessions,owner=c.owner,http=h,socket=sock)
    return ConnectionSupervisor(c.sessions,{'slack':adapter},owner=c.owner),adapter,h,ws


async def deliver(ws,body):
    await ws.queue.put(json.dumps(body))
    for _ in range(80):
        if any(x.get('envelope_id')==body['envelope_id'] for x in ws.sent):return
        await asyncio.sleep(.03)
    raise AssertionError('durable socket ACK not received')


def test_slack_native_socket_to_existing_qa_and_sdk_posts_once(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery,ChannelConnection
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();assert len(a.runs)==1
            with c.sessions() as db:assert all(db.get(ChannelConnection,cid).observed_state=='connected' for cid in c.ids['connections'])
            body=socket_frame();await deliver(ws,body)
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert len(rows)==1
                did=rows[0].id;assert rows[0].context_ciphertext and rows[0].status=='received'
            body['envelope_id']='redelivery-envelope';await deliver(ws,body)
            await a.step();await drain(a);await a.step();await drain(a)
            assert len(h.posts)==1 and h.posts[0]['channel']=='CQA001' and '정의한 로컬 답변' in h.posts[0]['text']
            with c.sessions() as db:
                row=db.get(ChannelConnectionDelivery,did);assert row.status=='sent' and row.queue_event_id and row.room_id and len(row.provider_message_ids)==1
        finally:await a.stop()
    asyncio.run(run())


def test_slack_busy_room_is_not_queued_to_answer_after_previous_question(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();await deliver(ws,socket_frame());await a.step();await drain(a)
            body=socket_frame();body['envelope_id']='second-question';await deliver(ws,body)
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert len(rows)==2
                refused=[x for x in rows if x.status=='failed' and x.last_error_code=='ROOM_BUSY'];assert len(refused)==1
            for _ in range(3):await a.step();await drain(a)
            assert len(h.posts)==1
        finally:await a.stop()
    asyncio.run(run())


async def finish(a):
    await a.step();await drain(a);await a.step();await drain(a)


def test_two_targets_share_one_socket_and_disconnect_independently(native_context):
    async def run():
        from app.models import ChannelConnection
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();await deliver(ws,socket_frame());await finish(a)
            b=socket_frame();b['envelope_id']='target-b';b['payload']['event']['channel']='CQA002';await deliver(ws,b);await finish(a)
            assert len(a.runs)==1 and len([v for v in h.calls if v[0]=='apps.connections.open'])==1 and len(h.posts)==2
            with c.sessions() as db:disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step();assert len(next(iter(a.runs.values())).targets)==1
            off=socket_frame();off['envelope_id']='off-a';await deliver(ws,off)
            b=socket_frame();b['envelope_id']='still-b';b['payload']['event']['channel']='CQA002';await deliver(ws,b);await finish(a)
            assert len(h.posts)==3 and h.posts[-1]['channel']=='CQA002'
        finally:await a.stop()
    asyncio.run(run())


def test_socket_threads_and_actors_reuse_separate_existing_rooms(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();one=socket_frame();one['payload']['event']['thread_ts']='1700000000.000001';await deliver(ws,one);await finish(a)
            two=socket_frame();two['envelope_id']='other-thread';two['payload']['event']['thread_ts']='1700000000.000002';two['payload']['event']['user']='UQA003';await deliver(ws,two);await finish(a)
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert len(rows)==2 and len({x.room_id for x in rows})==2
            assert [v['thread_ts'] for v in h.posts]==['1700000000.000001','1700000000.000002']
        finally:await a.stop()
    asyncio.run(run())


def test_unknown_slack_post_is_not_replayed_after_restart(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery,ChannelConnection
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h,ws=setup(c);b=None
        try:
            await a.step();await deliver(ws,socket_frame());h.fail=True;await finish(a)
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert len(rows)==1 and rows[0].status=='unknown';did=rows[0].id
                disconnect_connection(db.get(ChannelConnection,c.ids['connections'][1]));db.commit()
            await a.stop();other=subject().SlackAdapter(c.sessions,owner='c04-restarted',http=h,socket=Socket(Ws()));b=ConnectionSupervisor(c.sessions,{'slack':other},owner='c04-restarted')
            for _ in range(3):await b.step();await drain(b)
            with c.sessions() as db:
                assert db.get(ChannelConnectionDelivery,did).status=='unknown' and db.get(ChannelConnection,c.ids['connections'][1]).desired_state=='disconnected'
            assert len(h.posts)==1
        finally:
            if not a.stopping:await a.stop()
            if b:await b.stop()
    asyncio.run(run())


def test_another_worker_cannot_open_a_second_socket_for_the_same_app(native_context):
    async def run():
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h,ws=setup(c);sock=Socket(Ws());other=subject().SlackAdapter(c.sessions,owner='c04-other',http=NativeHttp(),socket=sock);b=ConnectionSupervisor(c.sessions,{'slack':other},owner='c04-other')
        try:
            await a.step();await b.step();assert len(a.runs)==1 and not b.runs and not sock.urls
        finally:await b.stop();await a.stop()
    asyncio.run(run())


def test_slack_buttons_keep_source_owner_and_restore_existing_variable(native_context):
    async def run():
        from copy import deepcopy
        from uuid import uuid4
        from app.models import BotVersion,ChannelConnectionDelivery
        c=native_context;a,adapter,h,ws=setup(c)
        with c.sessions() as db:
            version=db.get(BotVersion,c.ids['version_id']);doc=deepcopy(version.version_json);graph=doc['dialog_flow_graphs'][1];talk=next(n for n in graph['nodes'] if n['kind']=='talk')
            talk['config']={'messageType':'button','basicMessages':['선택하세요'],'messages':['확인','다음'],'responseType':'relay','responseVariableName':'choice'}
            graph['nodes'].append({'id':'slack-result','kind':'talk','config':{'messageType':'text','basicMessages':['선택: {{$choice}}'],'responseType':'none'}})
            for link in graph['links']:
                if link['sourceNodeId']==talk['id']:link['targetNodeId']='slack-result'
            graph['links'].append({'sourceNodeId':'slack-result','sourcePort':'next','targetNodeId':'local-end'});version.version_json=doc;db.commit()
        def action(data,owner='UQA002'):
            return {'type':'interactive','envelope_id':str(uuid4()),'payload':{'type':'block_actions','api_app_id':'AQA001','team':{'id':'TQA001'},'user':{'id':owner},'channel':{'id':'CQA001'},'container':{'message_ts':'1700000000.000001'},'actions':[{'type':'button','action_id':'cga:1','action_ts':f'{time.time():.6f}','value':json.dumps(data)}]}}
        try:
            await a.step();await deliver(ws,socket_frame());await finish(a)
            data=json.loads(next(v for v in h.posts[0]['blocks'] if v['type']=='actions')['elements'][1]['value'])
            for body in [action({**data,'messageId':str(uuid4())}),action(data,'UWRONG')]:
                await deliver(ws,body);await finish(a)
                with c.sessions() as db:
                    rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert sum(x.status=='failed' for x in rows)>=1 and not any(x.status=='unknown' for x in rows)
            correct=action(data);await deliver(ws,correct);await finish(a)
            assert len(h.posts)==2 and '선택: 다음' in h.posts[-1]['text']
            correct['envelope_id']='duplicate-button';await deliver(ws,correct);await finish(a);assert len(h.posts)==2
        finally:await a.stop()
    asyncio.run(run())
