import asyncio,json,time
from datetime import timedelta
from sqlalchemy import select
import pytest
from tests.test_channel_connection_runtime import native_context,test_engine,migrated_test_database
from tests.test_channel_connection_supervisor import drain
from tests.test_channel_connection_discord_gateway import Http,Ws,Socket,hello,ready


class NativeHttp(Http):
    def __init__(self):super().__init__();self.posts=[];self.acks=[];self.fail=False;self.fail_ack=False
    def call(self,method,path,token,payload=None):
        if path=='/gateway/bot':return {'url':'wss://gateway.discord.gg','shards':1,'session_start_limit':{'remaining':5,'reset_after':1000,'max_concurrency':1}}
        if method=='GET' and path.startswith('/channels/'):
            return {'id':path.split('/')[2],'guild_id':'300','type':0,'permission_overwrites':[]}
        if method=='POST':
            self.posts.append((path,payload))
            if self.fail:raise TimeoutError('synthetic uncertain result')
            return {'id':snowflake(),'channel_id':path.split('/')[2]}
        return super().call(method,path,token,payload)
    def ack(self,iid,token):
        self.acks.append(iid)
        if self.fail_ack:raise TimeoutError('synthetic uncertain ACK')


def snowflake(offset=0):return str(((int(time.time()*1000)-1420070400000)<<22)+offset)
def message(channel='400',actor='500',*,offset=0,addressed=True):return {'op':0,'t':'MESSAGE_CREATE','s':2+offset,'d':{'id':snowflake(offset),'channel_id':channel,'guild_id':'300','author':{'id':actor},'content':('<@200> ' if addressed else '')+'검증 질문','mentions':[{'id':'200'}] if addressed else []}}


def setup(c):
    from app.models import ChannelConnection,ChannelInstallationLease
    from app.services import channel_connection_state as state
    from app.services.channel_connections import encrypt_credentials
    from app.services.channel_connection_supervisor import ConnectionSupervisor
    from app.services.channel_connection_discord import DiscordAdapter
    from app.core.config import settings
    h=NativeHttp();ws=Ws([hello(),ready()])
    with c.sessions() as db:
        for i,cid in enumerate(c.ids['connections']):
            row=db.get(ChannelConnection,cid);row.installation_id='300';row.account_id='200';row.target_id=str(400+i);row.observed_state='connecting';row.lease_owner=None;row.lease_expires_at=None;row.accept_after=state.clock(db)-timedelta(seconds=1)
            row.credential_ciphertext=encrypt_credentials({'bot_token':'synthetic-only'},row.organization_id,row.id,settings.channel_connection_encryption_key,'v1');row.credential_key_version='v1'
        for lease in db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==c.ids['org_id'])):lease.lease_expires_at=state.clock(db)-timedelta(seconds=1)
        db.commit()
    adapter=DiscordAdapter(c.sessions,owner=c.owner,http=h,socket=Socket(ws))
    return ConnectionSupervisor(c.sessions,{'discord':adapter},owner=c.owner),adapter,h,ws


async def delivered(a,ws,frame):
    await ws.queue.put(json.dumps(frame))
    for _ in range(100):
        run=next(iter(a.runs.values()))
        if run.task.done():run.task.result()
        if run.listener.cursor.get('sequence',0)>=frame['s']:return
        await asyncio.sleep(.02)
    raise AssertionError('durable Gateway sequence missing')


async def finish(a):await a.step();await drain(a);await a.step();await drain(a)


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_gateway_existing_qa_posts_once_and_native_duplicate_is_not_reexecuted(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();assert len(a.runs)==1
            frame=message();await delivered(a,ws,frame);await finish(a)
            assert len(h.posts)==1 and h.posts[0][0]=='/channels/400/messages' and '정의한 로컬 답변' in h.posts[0][1]['content']
            frame['s']=3;await delivered(a,ws,frame);await finish(a);assert len(h.posts)==1
            with c.sessions() as db:
                row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0]));assert row.status=='sent' and row.room_id and row.queue_event_id
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_independent_targets_and_busy_question_is_never_executed_later(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery,ChannelConnection
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();await delivered(a,ws,message());await delivered(a,ws,message(actor='501',offset=1))
            await finish(a);assert len(h.posts)==1
            with c.sessions() as db:
                assert db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0],ChannelConnectionDelivery.last_error_code=='ROOM_BUSY'))
                disconnect_connection(db.get(ChannelConnection,c.ids['connections'][0]));db.commit()
            await a.step();await delivered(a,ws,message(channel='401',offset=2));await finish(a)
            assert len(h.posts)==2 and h.posts[-1][0]=='/channels/401/messages' and len(a.runs)==1
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_post_timeout_is_unknown_and_never_automatically_reposted(native_context):
    async def run():
        from app.models import ChannelConnectionDelivery
        c=native_context;a,adapter,h,ws=setup(c);h.fail=True
        try:
            await a.step();await delivered(a,ws,message());await finish(a)
            for _ in range(3):await finish(a)
            assert len(h.posts)==1
            with c.sessions() as db:assert db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])).status=='unknown'
        finally:await a.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
@pytest.mark.parametrize('unknown',[False,True])
def test_discord_button_ack_is_committed_before_execution_and_unknown_never_runs(native_context,unknown):
    async def run():
        from copy import deepcopy
        from app.models import BotVersion,ChannelConnectionDelivery
        from app.services.channel_connection_discord_gateway import DiscordState
        c=native_context;a,adapter,h,ws=setup(c)
        with c.sessions() as db:
            version=db.get(BotVersion,c.ids['version_id']);doc=deepcopy(version.version_json);graph=doc['dialog_flow_graphs'][1];talk=next(n for n in graph['nodes'] if n['kind']=='talk')
            talk['config']={'messageType':'button','basicMessages':['선택하세요'],'messages':['확인','다음'],'responseType':'relay','responseVariableName':'choice'}
            graph['nodes'].append({'id':'discord-result','kind':'talk','config':{'messageType':'text','basicMessages':['선택: {{$choice}}'],'responseType':'none'}})
            for link in graph['links']:
                if link['sourceNodeId']==talk['id']:link['targetNodeId']='discord-result'
            graph['links'].append({'sourceNodeId':'discord-result','sourcePort':'next','targetNodeId':'local-end'});version.version_json=doc;db.commit()
        observed=[];original=h.ack
        def ack(iid,token):
            current=next(iter(a.runs.values()));gate=current.listener.gate
            with c.sessions() as db:
                row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.event_key==__import__('hashlib').sha256(('discord-interaction:'+iid).encode()).hexdigest()))
                observed.append(row is not None and row.status=='received' and row.execution_claim_id is None and gate.context(row)['discord_ack']['status']=='sending')
            assert a._claim_work(current,next(iter(current.targets.values()))) is None
            return original(iid,token)
        h.ack=ack;h.fail_ack=unknown
        try:
            await a.step();await delivered(a,ws,message());await finish(a)
            custom=h.posts[0][1]['components'][0]['components'][1]['custom_id']
            iid=snowflake(10);frame={'op':0,'s':3,'t':'INTERACTION_CREATE','d':{'id':iid,'application_id':'100','type':3,'guild_id':'300','channel_id':'400','member':{'user':{'id':'500'}},'message':{'author':{'id':'200'}},'token':'synthetic-callback','data':{'component_type':2,'custom_id':custom}}}
            await delivered(a,ws,frame);await finish(a)
            assert observed==[True] and len(h.acks)==1 and len(h.posts)==(1 if unknown else 2)
            if not unknown:assert '선택: 다음' in h.posts[-1][1]['content']
            frame['s']=4;await delivered(a,ws,frame);await finish(a);assert len(h.acks)==1
            with c.sessions() as db:
                rows=list(db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==c.ids['connections'][0])));assert len(rows)==2
                button=next(x for x in rows if x.event_json['eventId'].startswith('discord-interaction:'));assert button.status==('unknown' if unknown else 'sent') and 'callback_token' not in next(iter(a.runs.values())).listener.gate.context(button)
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_resume_replays_uncommitted_event_without_changing_connection_cutoff(native_context):
    async def run():
        from app.models import ChannelConnection
        from app.services.channel_connection_discord import DiscordAdapter
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h,ws=setup(c);b=None
        try:
            await a.step();frame=message();await delivered(a,ws,frame);await finish(a)
            with c.sessions() as db:cutoff=db.get(ChannelConnection,c.ids['connections'][0]).accept_after
            missed=message(offset=1);await a.stop()
            refreshed=Ws([hello(),missed,{'op':0,'t':'RESUMED','s':4,'d':{}}]);other=DiscordAdapter(c.sessions,owner='c05-restarted',http=h,socket=Socket(refreshed));b=ConnectionSupervisor(c.sessions,{'discord':other},owner='c05-restarted')
            await b.step()
            for _ in range(100):
                run=next(iter(b.runs.values()))
                if run.task.done():run.task.result()
                if run.listener.cursor.get('sequence')==4:break
                await asyncio.sleep(.02)
            await finish(b);assert len(h.posts)==2 and refreshed.sent[0]['op']==6
            with c.sessions() as db:assert db.get(ChannelConnection,c.ids['connections'][0]).accept_after==cutoff
        finally:
            if not a.stopping:await a.stop()
            if b:await b.stop()
    asyncio.run(run())

@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_explicit_all_disconnect_discards_resume_state(native_context):
    async def run():
        from app.models import ChannelConnection,ChannelInstallationLease
        from app.services.channel_connections import disconnect_connection
        c=native_context;a,adapter,h,ws=setup(c)
        try:
            await a.step();run=next(iter(a.runs.values()))
            with c.sessions() as db:
                for cid in c.ids['connections']:disconnect_connection(db.get(ChannelConnection,cid))
                db.commit()
            await a.step();assert not a.runs
            with c.sessions() as db:assert 'discord_gateway' not in (db.get(ChannelInstallationLease,run.lease_id).cursor_json or {})
        finally:await a.stop()
    asyncio.run(run())


@pytest.mark.parametrize('native_context',['discord'],indirect=True)
def test_discord_same_app_cannot_be_opened_by_second_worker(native_context):
    async def run():
        from app.services.channel_connection_discord import DiscordAdapter
        from app.services.channel_connection_supervisor import ConnectionSupervisor
        c=native_context;a,adapter,h,ws=setup(c);socket=Socket(Ws([hello(),ready()]));other=DiscordAdapter(c.sessions,owner='c05-other',http=h,socket=socket);b=ConnectionSupervisor(c.sessions,{'discord':other},owner='c05-other')
        try:
            await a.step();await b.step();assert len(a.runs)==1 and not b.runs and not socket.urls
        finally:await b.stop();await a.stop()
    asyncio.run(run())
