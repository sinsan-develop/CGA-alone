"""기존 API 안에서 동작하는 연결 작업자. 실제 Provider는 단계별 adapter로 등록한다.

verify/open은 인증 및 수신 준비를 실제로 확인해야 한다. listen은 accept의 DB commit
완료 후에만 platform ACK/cursor를 확정한다. send는 자체 재시도/push 대체를 하지 않는다.
이 모듈에 대역 adapter를 등록하거나 외부 client 입력으로 registry를 변경하지 않는다.
"""
from __future__ import annotations
import asyncio
from datetime import timedelta
from dataclasses import dataclass,field
from types import SimpleNamespace
from typing import Protocol,Callable,Awaitable
from uuid import UUID,uuid4
from fastapi import HTTPException,Request
from sqlalchemy import select
from app.core.config import settings
from app.models import ChannelConnection,ChannelConnectionDelivery,ChannelInstallationLease
from app.services import channel_connection_state as state
from app.services.channel_connections import decrypt_credentials,encrypt_connection_state,decrypt_connection_state
from app.services.channel_connection_runtime import execute_connection_delivery
from app.services.channel_connection_bridge import convert_channel_packet


@dataclass(frozen=True)
class ConnectionSpec:
    id: UUID
    organization_id: UUID
    provider: str
    generation: int
    installation_id: str
    account_id: str
    target_id: str


@dataclass(frozen=True)
class VerifiedTarget:
    spec: ConnectionSpec
    installation_id: str
    account_id: str
    listener_scope: str
    transport: object = field(default=None,repr=False,compare=False)
    exclusive_scope: bool = False


@dataclass(frozen=True)
class InboundEnvelope:
    connection_id: UUID
    event: dict
    private_context: dict = field(default_factory=dict,repr=False)


class NativeHandshakePending(Exception):
    """외부 인증은 준비됐지만 실제 대상의 첫 검증 Activity를 기다린다."""


class NativeNotSubmitted(Exception):
    """실제 메시지 POST를 한 건도 시작하지 않았음을 전송 코드가 보장한다."""


class NativeListener(Protocol):
    async def update(self,targets:list[VerifiedTarget])->None: ...
    async def listen(self,accept:Callable[[list[InboundEnvelope]],Awaitable[list[UUID]]])->None: ...
    async def send(self,target:VerifiedTarget,delivery_id:UUID,packets:list,context:dict)->list[str]: ...
    async def close(self)->None: ...


class NativeAdapter(Protocol):
    async def verify(self,spec:ConnectionSpec,credentials:dict)->VerifiedTarget: ...
    async def open(self,targets:list[VerifiedTarget])->NativeListener: ...


# 소스에서만 등록한다. 실제 Provider 구현은 C03~C08에서 추가한다.
ADAPTERS:dict[str,NativeAdapter]={}


def execution_available(provider):
    return settings.channel_connection_worker_enabled and provider in ADAPTERS and bool(settings.channel_connection_encryption_key)


@dataclass
class ListenerRun:
    lease_id: UUID
    fence: int
    listener: NativeListener
    targets: dict[UUID,VerifiedTarget]
    task: asyncio.Task|None=None
    heartbeat: asyncio.Task|None=None


class ConnectionSupervisor:
    def __init__(self,sessions,adapters=None,*,owner=None,max_jobs=4,request=None):
        self.sessions=sessions;self.adapters=ADAPTERS if adapters is None else adapters
        self.owner=owner or 'channel-'+uuid4().hex;self.max_jobs=max(1,min(32,max_jobs))
        self.request=request or Request({'type':'http','method':'POST','path':'/internal/channel-connections','headers':[],'query_string':b'','scheme':'http','server':('internal',80),'client':None,'root_path':'','http_version':'1.1'})
        self.runs:dict[tuple,ListenerRun]={};self.verified={};self.jobs={};self.stopping=False
        self.lease_seconds=45

    def _snapshots(self):
        with self.sessions() as db:
            rows=db.scalars(select(ChannelConnection).where(ChannelConnection.deleted_at.is_(None),ChannelConnection.desired_state=='connected')).all()
            result=[]
            for r in rows:
                try:state._target_active(db,r)
                except HTTPException:
                    if r.lease_owner in {None,self.owner}:
                        r.observed_state='error';r.last_error_code='CONNECTION_TARGET_INACTIVE';r.lease_expires_at=None
                    continue
                result.append((ConnectionSpec(r.id,r.organization_id,r.provider,r.generation,r.installation_id,r.account_id,r.target_id),r.credential_ciphertext,r.credential_key_version))
            db.commit();return result

    def _credentials(self,spec,cipher,version):
        return decrypt_credentials(cipher,spec.organization_id,spec.id,settings.channel_connection_encryption_key,version)

    def _error(self,spec,code):
        with self.sessions() as db:
            row=db.get(ChannelConnection,spec.id)
            # 다른 프로세스의 유효한 소유권/새 명령에 오류를 덮어쓰지 않는다.
            if row and row.generation==spec.generation and row.desired_state=='connected' and (row.lease_owner in {None,self.owner} or row.lease_expires_at is None or row.lease_expires_at<=state.clock(db)):
                row.observed_state='error';row.last_error_code=code;row.lease_owner=None;row.lease_expires_at=None;row.accept_after=None;db.commit()

    def _pending(self,spec):
        with self.sessions() as db:
            row=state._connection(db,spec.id)
            if row.generation==spec.generation and row.desired_state=='connected' and row.lease_owner in {None,self.owner}:
                row.observed_state='connecting';row.last_error_code='WAITING_FIRST_ACTIVITY';db.commit()

    def _lease(self,targets):
        with self.sessions() as db:
            first=targets[0];row=state._connection(db,first.spec.id,lock=False)
            if row.generation!=first.spec.generation:return None
            lease=state.claim_installation(db,row,self.owner,ttl_seconds=self.lease_seconds,listener_scope=first.listener_scope,exclusive=first.exclusive_scope)
            if lease is None:db.commit();return None
            value=(lease.id,lease.fence);db.commit();return value

    def _activate(self,lease_id,fence,targets,resumed=False):
        with self.sessions() as db:
            state._installation(db,lease_id,self.owner,fence)
            live=[]
            for target in targets:
                row=state._connection(db,target.spec.id)
                if row.generation!=target.spec.generation:continue
                try:state._target_active(db,row)
                except HTTPException:continue
                if resumed and row.provider=='discord' and row.accept_after is not None:
                    row.observed_state='connecting';row.lease_owner=self.owner
                state.activate_connection(db,row.id,lease_id,self.owner,fence,{'installation_id':target.installation_id,'account_id':target.account_id,'listener_scope':target.listener_scope})
                live.append(target)
            db.commit();return live

    async def _close(self,key,run):
        self.runs.pop(key,None)
        tasks=[task for task in (run.task,run.heartbeat) if task]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        try:await asyncio.wait_for(run.listener.close(),5)
        except Exception:pass
        with self.sessions() as db:
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.id==run.lease_id).with_for_update())
            if lease and lease.owner==self.owner and lease.fence==run.fence:
                lease.lease_expires_at=state.clock(db)
                resumable=False
                for target in run.targets.values():
                    row=state._connection(db,target.spec.id)
                    if row.generation==target.spec.generation and row.desired_state=='connected':resumable=True
                    if row.generation==target.spec.generation and row.lease_owner==self.owner:
                        row.lease_expires_at=lease.lease_expires_at
                        if row.desired_state=='connected':
                            row.observed_state='connecting'
                            if row.provider!='discord':row.accept_after=None
                if lease.provider=='discord' and not resumable:
                    cursor=dict(lease.cursor_json or {});cursor.pop('discord_gateway',None);lease.cursor_json=cursor
                db.commit()

    async def heartbeat_once(self,run):
        def renew():
            with self.sessions() as db:
                lease=state._installation(db,run.lease_id,self.owner,run.fence)
                live=[]
                for target in list(run.targets.values()):
                    row=state._connection(db,target.spec.id)
                    if row.generation!=target.spec.generation:continue
                    try:state._scope_matches(row,lease);state._target_active(db,row)
                    except HTTPException:continue
                    live.append(row)
                if not live:return False
                lease.lease_expires_at=state.clock(db)+timedelta(seconds=self.lease_seconds)
                for row in live:row.lease_expires_at=lease.lease_expires_at
                db.commit();return True
        return await asyncio.to_thread(renew)

    async def _heartbeat(self,run):
        try:
            while True:
                await asyncio.sleep(10)
                if not await self.heartbeat_once(run):raise RuntimeError('LISTENER_HAS_NO_TARGET')
        except asyncio.CancelledError:raise
        except Exception:
            # 수신기가 확인할 권한을 잃으면 재조정 주기와 무관하게 즉시 중단한다.
            if run.task:run.task.cancel()
            try:await asyncio.wait_for(run.listener.close(),5)
            except Exception:pass
            raise RuntimeError('LISTENER_LEASE_LOST') from None

    async def _listen(self,key,run):
        await run.listener.listen(lambda envelopes:self.ingest(key,envelopes))
        raise RuntimeError('LISTENER_STOPPED')

    async def reconcile(self):
        grouped={};active_specs={}
        for spec,cipher,version in await asyncio.to_thread(self._snapshots):
            active_specs[spec.id]=spec
            adapter=self.adapters.get(spec.provider)
            if adapter is None:continue
            target=self.verified.get(spec.id)
            try:
                if target is None or target.spec!=spec:
                    credentials=await asyncio.to_thread(self._credentials,spec,cipher,version)
                    target=await asyncio.wait_for(adapter.verify(spec,credentials),15)
                    if (not isinstance(target,VerifiedTarget) or target.spec!=spec or target.installation_id!=spec.installation_id or target.account_id!=spec.account_id or not isinstance(target.listener_scope,str) or not target.listener_scope or len(target.listener_scope)>300):
                        raise ValueError('VERIFIED_IDENTITY_MISMATCH')
                    self.verified[spec.id]=target
                grouped.setdefault((spec.organization_id,spec.provider,target.listener_scope),[]).append(target)
            except NativeHandshakePending:
                await asyncio.to_thread(self._pending,spec)
            except Exception as error:
                # 공개 상태에는 허용된 고정 코드만 남기고 예외 원문은 버린다.
                code=str(getattr(error,'detail','')).split(':',1)[0]
                allowed={'TEAMS_APP_SCOPE_IN_USE','TEAMS_CONFIGURATION_INVALID','TEAMS_ACCESS_TOKEN_INVALID','TEAMS_TRANSPORT_FAILED','TEAMS_PROOF_SCOPE_MISMATCH'}
                await asyncio.to_thread(self._error,spec,code if code in allowed else 'CONNECTION_VERIFICATION_FAILED')
        self.verified={cid:target for cid,target in self.verified.items() if active_specs.get(cid)==target.spec}
        for key,run in list(self.runs.items()):
            if key not in grouped or run.task and run.task.done() or run.heartbeat and run.heartbeat.done():
                if run.task and run.task.done():
                    # 예외 원문은 토큰/사용자 입력을 포함할 수 있어 기록하지 않는다.
                    run.task.exception() if not run.task.cancelled() else None
                    for target in run.targets.values():self.verified.pop(target.spec.id,None)
                await self._close(key,run)
        for key,targets in grouped.items():
            run=self.runs.get(key)
            try:
                lease=await asyncio.to_thread(self._lease,targets)
                if lease is None:
                    if run:await self._close(key,run)
                    continue
                lease_id,fence=lease
                if run and (run.lease_id,run.fence)!=(lease_id,fence):
                    await self._close(key,run);run=None
                if run is None:
                    listener=await asyncio.wait_for(self.adapters[key[1]].open(targets),15)
                    run=ListenerRun(lease_id,fence,listener,{t.spec.id:t for t in targets})
                    self.runs[key]=run
                else:
                    # DB 검증이 선행해도 실제 수신은 현재 target+generation을 다시 검증한다.
                    run.targets={t.spec.id:t for t in targets}
                    await asyncio.wait_for(run.listener.update(targets),10)
                accepts=getattr(run.listener,'accepts_target',None)
                if callable(accepts):
                    eligible=[]
                    for target in targets:
                        if accepts(target):eligible.append(target)
                        else:await asyncio.to_thread(self._error,target.spec,'CONNECTION_CREDENTIALS_MISMATCH')
                    targets=eligible
                live=await asyncio.to_thread(self._activate,lease_id,fence,targets,getattr(run.listener,'resumed',False))
                if not live:
                    await self._close(key,run);continue
                run.targets={t.spec.id:t for t in live}
                if len(live)!=len(targets):await asyncio.wait_for(run.listener.update(live),10)
                if run.task is None:
                    run.task=asyncio.create_task(self._listen(key,run))
                    run.heartbeat=asyncio.create_task(self._heartbeat(run))
            except Exception:
                if run:await self._close(key,run)
                for target in targets:await asyncio.to_thread(self._error,target.spec,'CONNECTION_RECEIVER_UNAVAILABLE')

    async def ingest(self,key,envelopes):
        run=self.runs.get(key)
        if self.stopping or run is None or not isinstance(envelopes,list) or len(envelopes)>100:raise HTTPException(409,'LISTENER_UNAVAILABLE')
        targets=dict(run.targets)
        def commit():
            ids=[]
            with self.sessions() as db:
                for envelope in envelopes:
                    if not isinstance(envelope,InboundEnvelope) or envelope.connection_id not in targets:raise HTTPException(403,'CONNECTION_RECEIVER_SCOPE_MISMATCH')
                    target=targets[envelope.connection_id];row=state._live(db,target.spec.id,run.lease_id,self.owner,run.fence)
                    if row.generation!=target.spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
                    existing=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.event_key==state.scoped_hash(envelope.event['eventId']))) if row.provider in {'slack','discord'} else None
                    item=state.accept_delivery(db,row.id,run.lease_id,self.owner,run.fence,envelope.event)
                    if row.provider in {'slack','discord'} and existing is None and item.status=='received' and envelope.event['addressed']:
                        busy=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.id!=item.id,
                            ChannelConnectionDelivery.generation==row.generation,ChannelConnectionDelivery.native_room_hash==item.native_room_hash,
                            ChannelConnectionDelivery.status.in_(['received','executing','ready','sending','unknown']),ChannelConnectionDelivery.event_json['addressed'].astext=='true').limit(1))
                        if busy is not None:item.status='failed';item.last_error_code='ROOM_BUSY'
                    if envelope.private_context and item.context_ciphertext is None:
                        item.context_ciphertext=encrypt_connection_state(envelope.private_context,row.organization_id,row.id,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'delivery:'+str(item.id))
                        item.context_key_version=settings.channel_connection_key_version
                    ids.append(item.id)
                db.commit()
            return ids
        return await asyncio.to_thread(commit)

    def _claim_work(self,run,target):
        with self.sessions() as db:
            row=state._live(db,target.spec.id,run.lease_id,self.owner,run.fence)
            state.recover_expired(db,target.spec.id)
            if row.generation!=target.spec.generation:return None
            old=db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.status=='ready',ChannelConnectionDelivery.generation!=row.generation).with_for_update()).all()
            for item in old:item.status='cancelled';item.last_error_code='CONNECTION_CHANGED'
            ready=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.status=='ready',ChannelConnectionDelivery.generation==row.generation).order_by(ChannelConnectionDelivery.created_at).limit(1))
            if ready:
                value=('send',ready.id,None)
            else:
                item=state.claim_delivery(db,row.id,run.lease_id,self.owner,run.fence,deadline_seconds=300,can_execute=getattr(run.listener,'can_execute',None))
                value=('execute',item.id,item.execution_claim_id) if item else None
            db.commit();return value

    def _send_data(self,run,target,delivery_id):
        with self.sessions() as db:
            row=state._live(db,target.spec.id,run.lease_id,self.owner,run.fence);item=state._delivery(db,delivery_id)
            if row.generation!=target.spec.generation or item.generation!=row.generation or item.status!='ready':return None
            result=dict(item.result_json);event=dict(item.event_json)
            context=decrypt_connection_state(item.context_ciphertext,row.organization_id,row.id,settings.channel_connection_encryption_key,item.context_key_version,'delivery:'+str(item.id)) if item.context_ciphertext else {}
            db.commit()
        messages=result.get('botMessages',[])
        provider=target.spec.provider.replace('_','-')
        destination={k:event[k] for k in ('channelId','threadTs','chatId','threadId','spaceName','threadName') if k in event}
        packets=convert_channel_packet(provider,'replies',{'messages':messages,'destination':destination}) if provider in {'ms-teams','slack','discord','telegram','google-chat','line'} else messages
        with self.sessions() as db:
            claim=state.claim_send(db,delivery_id,run.lease_id,self.owner,run.fence);db.commit()
        return (claim,packets,context) if claim else None

    async def _send(self,run,target,delivery_id):
        claim=None
        try:
            data=await asyncio.to_thread(self._send_data,run,target,delivery_id)
            if data is None:return
            claim,packets,context=data
            # botMessages가 비면 일반 대화/무응답이다. 외부 게시 없이 결과를 확정한다.
            receipts=await asyncio.wait_for(run.listener.send(target,delivery_id,packets,context),25) if packets else []
            with self.sessions() as db:state.record_sent(db,delivery_id,claim,receipts);db.commit()
        except BaseException as error:
            with self.sessions() as db:
                if claim and isinstance(error,NativeNotSubmitted):
                    item=state._delivery(db,delivery_id)
                    if item.send_claim_id==claim and item.status in {'sending','unknown'}:
                        item.status='failed';item.last_error_code='DELIVERY_NOT_SUBMITTED';item.deadline_at=None
                elif claim:state.mark_unknown(db,delivery_id,claim,'DELIVERY_UNKNOWN')
                else:
                    item=state._delivery(db,delivery_id)
                    if item.status=='ready':item.status='failed';item.last_error_code='PACKET_NOT_SUBMITTED'
                db.commit()
            raise

    async def _job(self,work,run,target):
        kind,delivery_id,claim=work
        try:
            if kind=='execute':await asyncio.to_thread(execute_connection_delivery,self.sessions,delivery_id,run.lease_id,self.owner,run.fence,claim,self.request)
            else:await self._send(run,target,delivery_id)
        except Exception:pass  # 실행/전송 함수가 고정 상태를 영속화하며 예외 원문은 남기지 않는다.
        finally:self.jobs.pop(delivery_id,None)

    async def step(self):
        if self.stopping:return
        await self.reconcile()
        for run in list(self.runs.values()):
            for target in list(run.targets.values()):
                if len(self.jobs)>=self.max_jobs:return
                try:work=await asyncio.to_thread(self._claim_work,run,target)
                except HTTPException:continue
                if work and work[1] not in self.jobs:self.jobs[work[1]]=asyncio.create_task(self._job(work,run,target))

    async def serve(self):
        while not self.stopping:
            try:await self.step()
            except Exception:pass  # 다음 DB 확인까지 기다리며 이미 호출한 결과를 재실행하지 않는다.
            await asyncio.sleep(2)

    async def stop(self):
        self.stopping=True
        for key,run in list(self.runs.items()):await self._close(key,run)
        if self.jobs:
            # CGAスレッドは強制終了しない。結果保存は元の関数が完了させる。
            await asyncio.wait(list(self.jobs.values()),timeout=30)
