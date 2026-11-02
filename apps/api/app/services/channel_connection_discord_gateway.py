"""Discord 접수 확인과 Gateway 재개 정보를 기존 PostgreSQL 원장에 보호 저장한다."""
from __future__ import annotations
from dataclasses import dataclass,field
from uuid import uuid4
import math,re
from sqlalchemy import select
from sqlalchemy.orm import object_session
from fastapi import HTTPException
from app.core.config import settings
from app.models import ChannelConnection,ChannelConnectionDelivery,ChannelInstallationLease
from app.services import channel_connection_state as state
from app.services.channel_connections import encrypt_connection_state,decrypt_connection_state


@dataclass(frozen=True)
class AckReservation:
    claim_id: str
    interaction_id: str
    callback_token: str=field(repr=False)


class DiscordState:
    def __init__(self,sessions,*,lease_id,owner,fence):
        self.sessions=sessions;self.lease_id=lease_id;self.owner=owner;self.fence=fence

    def _lease(self,db):
        lease=state._installation(db,self.lease_id,self.owner,self.fence)
        if lease.provider!='discord':raise HTTPException(409,'INSTALLATION_SCOPE_MISMATCH')
        return lease

    def context(self,item):
        if not item.context_ciphertext:return {}
        return decrypt_connection_state(item.context_ciphertext,item.organization_id,item.connection_id,settings.channel_connection_encryption_key,item.context_key_version,'delivery:'+str(item.id))

    def _write(self,item,value):
        version=settings.channel_connection_key_version
        item.context_ciphertext=encrypt_connection_state(value,item.organization_id,item.connection_id,settings.channel_connection_encryption_key,version,'delivery:'+str(item.id));item.context_key_version=version

    def _terminal(self,item,ctx,status,code):
        item.status=status;item.last_error_code=code
        ctx.pop('callback_token',None);ctx['discord_ack']['status']=status;self._write(item,ctx)

    def can_execute(self,item):
        if item.status!='received':return False
        ctx=self.context(item);ack=ctx.get('discord_ack')
        if ack is None:return True
        if ack.get('status')=='confirmed':return True
        if ack.get('status') in {'pending','sending'}:
            expires=ack.get('expires_at');db=object_session(item)
            if not isinstance(expires,(int,float)) or not math.isfinite(expires) or expires<=state.clock(db).timestamp():
                unknown=ack.get('status')=='sending'
                self._terminal(item,ctx,'unknown' if unknown else 'failed','DISCORD_ACK_UNKNOWN' if unknown else 'DISCORD_ACK_EXPIRED')
        return False

    def _locked(self,db,did,lease):
        initial=db.get(ChannelConnectionDelivery,did)
        if initial is None or initial.organization_id!=lease.organization_id:return None,None
        row=db.scalar(select(ChannelConnection).where(ChannelConnection.id==initial.connection_id).with_for_update())
        item=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.id==did).with_for_update().execution_options(populate_existing=True))
        if row is None or row.organization_id!=lease.organization_id or row.provider!='discord':return None,None
        state._scope_matches(row,lease)
        return row,item

    def reserve_ack(self,did):
        with self.sessions() as db:
            lease=self._lease(db);row,item=self._locked(db,did,lease)
            if item is None:return None
            ctx=self.context(item);ack=ctx.get('discord_ack',{})
            if item.status!='received' or ack.get('status')!='pending':
                if item.status in {'failed','cancelled'} and 'callback_token' in ctx:
                    ctx.pop('callback_token',None);self._write(item,ctx);db.commit()
                return None
            if row.deleted_at is not None or row.generation!=item.generation or row.desired_state!='connected':
                self._terminal(item,ctx,'cancelled','CONNECTION_CHANGED');db.commit();return None
            state._live(db,row.id,self.lease_id,self.owner,self.fence)
            self.can_execute(item)
            if item.status!='received':db.commit();return None
            token=ctx.get('callback_token');iid=ctx.get('interaction_id')
            if not isinstance(token,str) or not token or not isinstance(iid,str) or not re.fullmatch('[1-9][0-9]{0,19}',iid):
                self._terminal(item,ctx,'failed','DISCORD_ACK_INVALID');db.commit();return None
            claim=str(uuid4());ack.update(status='sending',claim_id=claim);self._write(item,ctx);db.commit()
            return AckReservation(claim,iid,token)

    def finish_ack(self,did,claim_id,outcome):
        if outcome not in {'confirmed','unknown','rejected'}:raise ValueError('INVALID_ACK_OUTCOME')
        with self.sessions() as db:
            # 네트워크 후의 확정 영수증은 lease 만료 후에도 같은 claim으로만 보존한다.
            lease=db.get(ChannelInstallationLease,self.lease_id)
            if lease is None or lease.provider!='discord':return False
            row,item=self._locked(db,did,lease)
            if item is None:return False
            ctx=self.context(item);ack=ctx.get('discord_ack',{})
            if item.status!='received' or ack.get('status')!='sending' or ack.get('claim_id')!=claim_id:return False
            if row.deleted_at is not None or row.generation!=item.generation or row.desired_state!='connected':
                self._terminal(item,ctx,'cancelled','CONNECTION_CHANGED')
            elif outcome=='confirmed':
                ctx.pop('callback_token',None);ack['status']='confirmed';self._write(item,ctx)
            else:self._terminal(item,ctx,'unknown' if outcome=='unknown' else 'failed','DISCORD_ACK_UNKNOWN' if outcome=='unknown' else 'DISCORD_ACK_REJECTED')
            db.commit();return True

    def save_cursor(self,value):
        _validate_cursor(value)
        with self.sessions() as db:
            lease=self._lease(db)
            cursor=dict(lease.cursor_json or {})
            version=settings.channel_connection_key_version
            cursor['discord_gateway']={'ciphertext':encrypt_connection_state(value,lease.organization_id,lease.id,settings.channel_connection_encryption_key,version,'discord-gateway'),'key_version':version}
            lease.cursor_json=cursor;db.commit()

    def load_cursor(self):
        with self.sessions() as db:
            lease=self._lease(db);saved=(lease.cursor_json or {}).get('discord_gateway')
            if saved is None:return None
            value=decrypt_connection_state(saved['ciphertext'],lease.organization_id,lease.id,settings.channel_connection_encryption_key,saved['key_version'],'discord-gateway')
            _validate_cursor(value);return value

import asyncio,json,hashlib,logging,time
from urllib.parse import urlsplit,parse_qs
from app.services.channel_connection_supervisor import InboundEnvelope,NativeNotSubmitted
from app.services.channel_connection_bridge import convert_channel_packet,NativePacketRejected
from datetime import datetime,timezone


def validate_gateway_url(value):
    try:
        if not isinstance(value,str) or len(value)>2048 or any(c.isspace() for c in value) or '\\' in value:raise ValueError()
        p=urlsplit(value);q=parse_qs(p.query,strict_parsing=True)
        if p.scheme!='wss' or not re.fullmatch(r'gateway(?:-[a-z0-9]+)*\.discord\.gg',p.hostname or '') or p.port not in {None,443} or p.username is not None or p.password is not None or p.path not in {'','/'} or p.fragment or any(k not in {'v','encoding'} for k in q) or q.get('v',['10'])!=['10'] or q.get('encoding',['json'])!=['json']:raise ValueError()
        return 'wss://'+p.hostname+'/?v=10&encoding=json'
    except (ValueError,TypeError):raise HTTPException(403,'DISCORD_GATEWAY_DESTINATION_UNTRUSTED') from None



def _validate_cursor(value):
    fields={'application_id','bot_user_id','token_hash','session_id','resume_url','sequence'}
    if value=={}:return
    if not isinstance(value,dict) or set(value)!=fields:raise HTTPException(422,'DISCORD_CURSOR_INVALID')
    if any(not isinstance(value[k],str) or not re.fullmatch('[1-9][0-9]{0,19}',value[k]) for k in ['application_id','bot_user_id']):raise HTTPException(422,'DISCORD_CURSOR_INVALID')
    if not isinstance(value['token_hash'],str) or not re.fullmatch('[0-9a-f]{64}',value['token_hash']) or not isinstance(value['session_id'],str) or not 0<len(value['session_id'])<=512 or type(value['sequence']) is not int or not 0<=value['sequence']<=9223372036854775807:raise HTTPException(422,'DISCORD_CURSOR_INVALID')
    validate_gateway_url(value['resume_url'])


def _frame(raw):
    try:
        if not isinstance(raw,(str,bytes)) or len(raw.encode() if isinstance(raw,str) else raw)>1048576:raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict) or type(value.get('op')) is not int:raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError):raise HTTPException(422,'DISCORD_FRAME_INVALID') from None


class DiscordSocketTransport:
    async def open(self,url):
        from websockets.asyncio.client import connect
        from websockets.exceptions import SecurityError
        class StrictConnect(connect):
            def process_redirect(self,error):return SecurityError('DISCORD_GATEWAY_REDIRECT_FORBIDDEN')
        logger=logging.getLogger('cga.private.discord.websocket');logger.disabled=True;logger.propagate=False
        try:return await StrictConnect(validate_gateway_url(url),proxy=None,open_timeout=5,ping_interval=None,close_timeout=2,max_size=1048576,max_queue=8,compression=None,logger=logger)
        except Exception:raise HTTPException(503,'DISCORD_GATEWAY_UNAVAILABLE') from None


async def open_gateway(adapter,targets,gate):
    if not targets or any(t.listener_scope!=targets[0].listener_scope for t in targets):raise HTTPException(403,'DISCORD_GATEWAY_SCOPE_MISMATCH')
    binding=targets[0].transport;token=binding.credentials['bot_token'];app=binding.application_id
    if adapter.open_not_before.get(app,0)>time.monotonic():raise HTTPException(503,'DISCORD_GATEWAY_RECONNECT_DELAY')
    adapter.open_not_before[app]=time.monotonic()+5
    ws=None;listener=None
    try:
        info=await asyncio.to_thread(adapter.http.call,'GET','/gateway/bot',token)
        if info.get('shards')!=1:raise ValueError()
        saved=await asyncio.to_thread(gate.load_cursor)
        digest=hashlib.sha256(token.encode()).hexdigest()
        resumable=bool(saved and (saved.get('application_id'),saved.get('bot_user_id'),saved.get('token_hash'))==(app,binding.bot_user_id,digest) and isinstance(saved.get('session_id'),str) and saved['session_id'] and type(saved.get('sequence')) is int and saved['sequence']>=0)
        url=validate_gateway_url(saved['resume_url'] if resumable else info.get('url'))
        if not resumable:
            limit=info.get('session_start_limit',{})
            if type(limit.get('remaining')) is not int or limit['remaining']<=0:
                reset=limit.get('reset_after',60000)
                adapter.open_not_before[app]=time.monotonic()+max(5,min(86400,reset/1000 if isinstance(reset,(int,float)) else 60));raise ValueError()
        ws=await adapter.socket.open(url);hello=_frame(await asyncio.wait_for(ws.recv(),5))
        interval=(hello.get('d') or {}).get('heartbeat_interval')
        if hello['op']!=10 or type(interval) not in {int,float} or not math.isfinite(interval) or not 100<=interval<=300000:raise ValueError()
        listener=DiscordListener(adapter,targets,ws,gate,interval/1000,digest,saved if resumable else None)
        if resumable:packet={'op':6,'d':{'token':token,'session_id':saved['session_id'],'seq':saved['sequence']}}
        else:packet={'op':2,'d':{'token':token,'intents':4609,'properties':{'os':'linux','browser':'CGA-alone','device':'CGA-alone'}}}
        if len(json.dumps(packet).encode())>4096:raise ValueError()
        await ws.send(json.dumps(packet));listener.heartbeat=asyncio.create_task(listener._heartbeat())
        async with asyncio.timeout(10):
            size=0
            while True:
                frame=_frame(await ws.recv())
                if await listener.control(frame):continue
                if frame['op']!=0:raise ValueError()
                seq=frame.get('s')
                if type(seq) is not int or seq<0:raise ValueError()
                listener.received_sequence=seq
                if not resumable:
                    data=frame.get('d') or {}
                    if frame.get('t')!='READY' or (data.get('user') or {}).get('id')!=binding.bot_user_id or (data.get('user') or {}).get('bot') is not True or (data.get('application') or {}).get('id')!=app:raise ValueError()
                    sid=data.get('session_id')
                    if not isinstance(sid,str) or not 0<len(sid)<=512:raise ValueError()
                    listener.cursor={'application_id':app,'bot_user_id':binding.bot_user_id,'token_hash':digest,'session_id':sid,'resume_url':validate_gateway_url(data.get('resume_gateway_url')),'sequence':seq}
                    await asyncio.to_thread(gate.save_cursor,listener.cursor);return listener
                size+=len(json.dumps(frame).encode())
                if len(listener.buffer)>=64 or size>1048576 or seq<=saved['sequence']:raise ValueError()
                listener.buffer.append(frame)
                if frame.get('t')=='RESUMED':listener.resumed=True;return listener
    except BaseException as error:
        if listener is not None:await listener.close()
        elif ws is not None:
            try:await ws.close(code=4000)
            except Exception:pass
        if isinstance(error,asyncio.CancelledError):raise
        raise HTTPException(503,'DISCORD_GATEWAY_HANDSHAKE_FAILED') from None


class DiscordListener:
    def __init__(self,adapter,targets,ws,gate,interval,digest,cursor):
        self.adapter=adapter;self.ws=ws;self.gate=gate;self.interval=interval;self.digest=digest;self.app_id=targets[0].transport.application_id
        self.targets={t.spec.id:t for t in targets if self.accepts_target(t)};self.cursor=dict(cursor or {});self.received_sequence=self.cursor.get('sequence');self.awaiting_ack=False;self.stopped=False;self.heartbeat=None;self.resumed=False;self.buffer=[]
    def accepts_target(self,target):return target.transport.application_id==self.app_id and hashlib.sha256(target.transport.credentials['bot_token'].encode()).hexdigest()==self.digest
    def can_execute(self,item):return self.gate.can_execute(item)
    async def update(self,targets):self.targets={t.spec.id:t for t in targets if self.accepts_target(t)}
    async def close(self):
        self.stopped=True
        if self.heartbeat and self.heartbeat is not asyncio.current_task():
            self.heartbeat.cancel();await asyncio.gather(self.heartbeat,return_exceptions=True)
        await self.ws.close(code=4000)
    async def heartbeat_once(self):
        if self.awaiting_ack:raise RuntimeError('DISCORD_HEARTBEAT_ACK_MISSING')
        await self.ws.send(json.dumps({'op':1,'d':self.received_sequence}));self.awaiting_ack=True
    async def _heartbeat(self):
        try:
            while not self.stopped:
                await asyncio.sleep(self.interval);await self.heartbeat_once()
        except asyncio.CancelledError:raise
        except Exception:
            self.stopped=True;await self.ws.close(code=4000)
    async def control(self,frame):
        op=frame['op']
        if op==11:self.awaiting_ack=False;return True
        if op==1:
            await self.ws.send(json.dumps({'op':1,'d':self.received_sequence}));self.awaiting_ack=True;return True
        if op==9:
            if frame.get('d') is not True:await asyncio.to_thread(self.gate.save_cursor,{})
            raise RuntimeError('DISCORD_SESSION_INVALID')
        if op==7:raise RuntimeError('DISCORD_RESUME_REQUIRED')
        return False
    async def listen(self,accept):
        while not self.stopped:
            frame=self.buffer.pop(0) if self.buffer else _frame(await self.ws.recv())
            if await self.control(frame):continue
            await self.handle(frame,accept)
    async def handle(self,frame,accept):
        if self.stopped:raise HTTPException(409,'DISCORD_LISTENER_STOPPED')
        seq=frame.get('s')
        if frame.get('op')!=0 or type(seq) is not int or seq<0:raise HTTPException(422,'DISCORD_FRAME_INVALID')
        self.received_sequence=seq
        if seq<=self.cursor.get('sequence',-1):return
        try:await self._dispatch(frame,accept)
        except NativePacketRejected:pass
        except HTTPException as error:
            if error.status_code not in {404,409} or error.detail not in {'CONNECTION_CHANGED','CONNECTION_TARGET_INACTIVE','CONNECTION_NOT_FOUND'}:raise
        self.cursor={**self.cursor,'sequence':seq};await asyncio.to_thread(self.gate.save_cursor,self.cursor)
    async def _dispatch(self,frame,accept):
        if frame.get('t') not in {'MESSAGE_CREATE','INTERACTION_CREATE'}:return
        data=frame.get('d')
        if not isinstance(data,dict):raise HTTPException(422,'DISCORD_FRAME_INVALID')
        channel=data.get('channel_id') or (data.get('channel') or {}).get('id');guild=data.get('guild_id')
        targets=[t for t in self.targets.values() if (t.transport.guild_id,t.transport.channel_id)==(guild,channel)]
        if len(targets)>1:raise HTTPException(409,'DISCORD_TARGET_AMBIGUOUS')
        if not targets:return
        target=targets[0];binding=target.transport
        normalized=await asyncio.to_thread(convert_channel_packet,'discord','normalize',{'input':frame,'connection':{'applicationId':binding.application_id,'botUserId':binding.bot_user_id,'guildId':binding.guild_id,'channelId':binding.channel_id,'channelType':binding.channel_type}})
        if normalized is None:return
        # 모든 이벤트는 Discord snowflake 시각을 사용한다. 클라이언트 시각/ID를 신뢰하지 않는다.
        identifier=data.get('id')
        if not isinstance(identifier,str) or not re.fullmatch('[1-9][0-9]{0,19}',identifier):raise HTTPException(422,'DISCORD_FRAME_INVALID')
        stamp=((int(identifier)>>22)+1420070400000)/1000
        normalized['occurredAt']=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        context={'application_id':binding.application_id,'bot_user_id':binding.bot_user_id,'guild_id':guild,'channel_id':channel,'generation':target.spec.generation}
        interaction=frame['t']=='INTERACTION_CREATE'
        if interaction:
            token=data.get('token')
            if not isinstance(token,str) or not token or len(token)>24000 or any(c.isspace() for c in token):raise HTTPException(422,'DISCORD_CALLBACK_INVALID')
            context.update(interaction_id=identifier,callback_token=token,discord_ack={'status':'pending','expires_at':stamp+3})
        committed=await accept([InboundEnvelope(target.spec.id,normalized,context)])
        if not isinstance(committed,list) or len(committed)!=1:raise RuntimeError('DISCORD_COMMIT_UNCONFIRMED')
        if interaction:
            reservation=await asyncio.to_thread(self.gate.reserve_ack,committed[0])
            if reservation is not None:
                outcome='unknown'
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.adapter.http.ack,reservation.interaction_id,reservation.callback_token),2)
                    outcome='confirmed'
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.to_thread(self.gate.finish_ack,committed[0],reservation.claim_id,'unknown'));raise
                except Exception as error:
                    from app.services.channel_connection_discord import DiscordApiRejected
                    if isinstance(error,DiscordApiRejected):outcome='rejected'
                await asyncio.to_thread(self.gate.finish_ack,committed[0],reservation.claim_id,outcome)

    def _assert_live(self,target,delivery_id):
        with self.adapter.sessions() as db:
            row=state._live(db,target.spec.id,self.gate.lease_id,self.adapter.owner,self.gate.fence);item=state._delivery(db,delivery_id)
            if row.generation!=target.spec.generation or item.connection_id!=row.id or item.generation!=row.generation or item.status!='sending' or item.installation_fence!=self.gate.fence:raise HTTPException(409,'CONNECTION_CHANGED')
            db.commit()

    async def send(self,target,delivery_id,packets,context):
        from app.services.channel_connection_discord import DiscordApiRejected,EMBED
        try:
            b=target.transport
            if self.stopped or self.targets.get(target.spec.id)!=target or (context.get('application_id'),context.get('bot_user_id'),context.get('guild_id'),context.get('channel_id'),context.get('generation'))!=(b.application_id,b.bot_user_id,b.guild_id,b.channel_id,target.spec.generation):raise ValueError()
            if not isinstance(packets,list) or len(packets)>20:raise ValueError()
            bodies=[]
            for packet in packets:
                if not isinstance(packet,dict) or set(packet)-{'channel_id','content','allowed_mentions','embeds','components'} or packet.get('channel_id')!=b.channel_id or len(json.dumps(packet,ensure_ascii=False).encode())>1000000:raise ValueError()
                if packet.get('allowed_mentions')!={'parse':[],'replied_user':False} or packet.get('embeds') and not b.permissions&EMBED:raise ValueError()
                bodies.append({k:v for k,v in packet.items() if k!='channel_id'})
        except (ValueError,TypeError,KeyError):raise NativeNotSubmitted() from None
        receipts=[]
        for body in bodies:
            try:
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
                await asyncio.to_thread(self._assert_live,target,delivery_id)
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
            except (HTTPException,ValueError):
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('DISCORD_SEND_REVOKED') from None
            try:result=await asyncio.to_thread(self.adapter.http.call,'POST','/channels/'+b.channel_id+'/messages',b.credentials['bot_token'],body)
            except DiscordApiRejected:
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('DISCORD_PARTIAL_DELIVERY') from None
            identifier=result.get('id')
            if result.get('channel_id')!=b.channel_id or not isinstance(identifier,str) or not re.fullmatch('[1-9][0-9]{0,19}',identifier):raise RuntimeError('DISCORD_RECEIPT_UNCONFIRMED')
            receipts.append(b.channel_id+':'+identifier)
        return receipts
