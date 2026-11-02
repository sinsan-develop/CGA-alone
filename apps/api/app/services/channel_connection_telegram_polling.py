"""Telegram polling의 offset/접수 상태를 기존 PostgreSQL lease·원장에 보호 저장한다."""
from __future__ import annotations
import asyncio,json,math,time,hashlib
from dataclasses import dataclass,field
from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import object_session
from fastapi import HTTPException
from app.core.config import settings
from app.models import ChannelConnection,ChannelConnectionDelivery,ChannelInstallationLease
from app.services import channel_connection_state as state
from app.services.channel_connections import encrypt_connection_state,decrypt_connection_state


@dataclass(frozen=True)
class CallbackReservation:
    claim_id:str
    callback_id:str=field(repr=False)


class TelegramState:
    def __init__(self,sessions,*,lease_id,owner,fence,bot_id):self.sessions=sessions;self.lease_id=lease_id;self.owner=owner;self.fence=fence;self.bot_id=bot_id
    def lease(self,db):
        lease=state._installation(db,self.lease_id,self.owner,self.fence)
        if lease.provider!='telegram':raise HTTPException(409,'INSTALLATION_SCOPE_MISMATCH')
        return lease
    def _load(self,lease):
        saved=(lease.cursor_json or {}).get('telegram_polling')
        if saved is None:return {'bot_id':self.bot_id,'offset':0,'targets':{}}
        value=decrypt_connection_state(saved['ciphertext'],lease.organization_id,lease.id,settings.channel_connection_encryption_key,saved['key_version'],'telegram-polling')
        if value.get('bot_id')!=self.bot_id or type(value.get('offset')) is not int or value['offset']<0 or not isinstance(value.get('targets'),dict):raise HTTPException(409,'TELEGRAM_CURSOR_INVALID')
        return value
    def _save(self,lease,value):
        cursor=dict(lease.cursor_json or {});version=settings.channel_connection_key_version
        cursor['telegram_polling']={'ciphertext':encrypt_connection_state(value,lease.organization_id,lease.id,settings.channel_connection_encryption_key,version,'telegram-polling'),'key_version':version};lease.cursor_json=cursor
    def load_cursor(self):
        with self.sessions() as db:return self._load(self.lease(db))
    def advance(self,update_id):
        if type(update_id) is not int or not 0<=update_id<9007199254740991:raise HTTPException(422,'TELEGRAM_UPDATE_INVALID')
        with self.sessions() as db:
            lease=self.lease(db);value=self._load(lease);value['offset']=max(value['offset'],update_id+1);self._save(lease,value);db.commit()
    def prepare_targets(self,targets):
        with self.sessions() as db:
            lease=self.lease(db);value=self._load(lease);boundary=0
            for target in sorted(targets,key=lambda t:str(t.spec.id)):
                row=state._connection(db,target.spec.id)
                if row.organization_id!=lease.organization_id or row.provider!='telegram' or state.scoped_hash('verified-listener:telegram:'+target.listener_scope)!=lease.listener_scope_hash:raise HTTPException(409,'INSTALLATION_SCOPE_MISMATCH')
                if row.generation!=target.spec.generation or row.desired_state!='connected':continue
                try:state._target_active(db,row)
                except HTTPException:continue
                saved=value['targets'].get(str(row.id))
                if isinstance(saved,dict) and saved.get('generation')==row.generation and saved.get('ready') is True:
                    cutoff=datetime.fromisoformat(saved['cutoff'])
                    if cutoff.tzinfo is None:raise HTTPException(409,'TELEGRAM_CURSOR_INVALID')
                else:
                    stamp=math.ceil(state.clock(db).timestamp());cutoff=datetime.fromtimestamp(stamp,timezone.utc);boundary=max(boundary,stamp)
                    value['targets'][str(row.id)]={'generation':row.generation,'cutoff':cutoff.isoformat(),'ready':False}
                row.accept_after=cutoff
                if not isinstance(saved,dict) or saved.get('generation')!=row.generation or row.observed_state!='connected' or row.lease_owner!=self.owner or row.lease_expires_at is None or row.lease_expires_at<=state.clock(db):
                    row.observed_state='connecting';row.lease_owner=self.owner
            self._save(lease,value);db.commit();return boundary
    def mark_ready(self,targets):
        with self.sessions() as db:
            lease=self.lease(db);value=self._load(lease)
            for target in targets:
                saved=value['targets'].get(str(target.spec.id))
                if isinstance(saved,dict) and saved.get('generation')==target.spec.generation:saved['ready']=True
            self._save(lease,value);db.commit()
    def context(self,item):
        return decrypt_connection_state(item.context_ciphertext,item.organization_id,item.connection_id,settings.channel_connection_encryption_key,item.context_key_version,'delivery:'+str(item.id)) if item.context_ciphertext else {}
    def write_context(self,item,value):
        version=settings.channel_connection_key_version;item.context_ciphertext=encrypt_connection_state(value,item.organization_id,item.connection_id,settings.channel_connection_encryption_key,version,'delivery:'+str(item.id));item.context_key_version=version
    def terminal(self,item,ctx,status,code):
        item.status=status;item.last_error_code=code;ctx.pop('callback_id',None);ctx['telegram_ack']['status']=status;self.write_context(item,ctx)
    def can_execute(self,item):
        if item.status!='received':return False
        ctx=self.context(item);ack=ctx.get('telegram_ack')
        if ack is None:return True
        if ack.get('status')=='confirmed':return True
        deadline=ack.get('expires_at')
        if ack.get('status') in {'pending','sending'} and (type(deadline) not in {int,float} or not math.isfinite(deadline) or deadline<=state.clock(object_session(item)).timestamp()):
            unknown=ack.get('status')=='sending';self.terminal(item,ctx,'unknown' if unknown else 'failed','TELEGRAM_ACK_UNKNOWN' if unknown else 'TELEGRAM_ACK_EXPIRED')
        return False
    def locked(self,db,did,lease):
        initial=db.get(ChannelConnectionDelivery,did)
        if initial is None or initial.organization_id!=lease.organization_id:return None,None
        row=db.scalar(select(ChannelConnection).where(ChannelConnection.id==initial.connection_id).with_for_update())
        item=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.id==did).with_for_update().execution_options(populate_existing=True))
        if row is None or row.provider!='telegram' or row.organization_id!=lease.organization_id:return None,None
        state._scope_matches(row,lease);return row,item
    def reserve_ack(self,did):
        with self.sessions() as db:
            lease=self.lease(db);row,item=self.locked(db,did,lease)
            if item is None:return None
            ctx=self.context(item);ack=ctx.get('telegram_ack',{})
            if item.status!='received' or ack.get('status')!='pending':
                if item.status in {'failed','cancelled'} and 'callback_id' in ctx:ctx.pop('callback_id');self.write_context(item,ctx);db.commit()
                return None
            if row.deleted_at is not None or row.generation!=item.generation or row.desired_state!='connected':self.terminal(item,ctx,'cancelled','CONNECTION_CHANGED');db.commit();return None
            state._live(db,row.id,self.lease_id,self.owner,self.fence);self.can_execute(item)
            if item.status!='received':db.commit();return None
            identifier=ctx.get('callback_id')
            if not isinstance(identifier,str) or not 0<len(identifier)<=200:self.terminal(item,ctx,'failed','TELEGRAM_ACK_INVALID');db.commit();return None
            claim=str(uuid4());ack.update(status='sending',claim_id=claim);self.write_context(item,ctx);db.commit();return CallbackReservation(claim,identifier)
    def finish_ack(self,did,claim_id,outcome):
        if outcome not in {'confirmed','unknown','rejected'}:raise ValueError('INVALID_ACK_OUTCOME')
        with self.sessions() as db:
            lease=db.get(ChannelInstallationLease,self.lease_id)
            if lease is None or lease.provider!='telegram':return False
            row,item=self.locked(db,did,lease)
            if item is None:return False
            ctx=self.context(item);ack=ctx.get('telegram_ack',{})
            if item.status!='received' or ack.get('status')!='sending' or ack.get('claim_id')!=claim_id:return False
            if row.deleted_at is not None or row.generation!=item.generation or row.desired_state!='connected':self.terminal(item,ctx,'cancelled','CONNECTION_CHANGED')
            elif outcome=='confirmed':ctx.pop('callback_id',None);ack['status']='confirmed';self.write_context(item,ctx)
            else:self.terminal(item,ctx,'unknown' if outcome=='unknown' else 'failed','TELEGRAM_ACK_UNKNOWN' if outcome=='unknown' else 'TELEGRAM_ACK_REJECTED')
            db.commit();return True

from app.services.channel_connection_supervisor import InboundEnvelope,NativeNotSubmitted
from app.services.channel_connection_bridge import convert_channel_packet,NativePacketRejected


class TelegramListener:
    def __init__(self,adapter,targets,gate,digest):
        self.adapter=adapter;self.gate=gate;self.digest=digest;self.bot_id=targets[0].transport.bot_user_id;self.stopped=False;self.buffer=[];self.offset=0
        self.targets={t.spec.id:t for t in targets if self.accepts_target(t)}
    def accepts_target(self,target):return target.transport.bot_user_id==self.bot_id and hashlib.sha256(target.transport.credentials['bot_token'].encode()).hexdigest()==self.digest
    def can_execute(self,item):return self.gate.can_execute(item)
    async def prepare(self,targets):
        allowed=[t for t in targets if self.accepts_target(t)]
        boundary=await asyncio.to_thread(self.gate.prepare_targets,allowed)
        if boundary:await asyncio.sleep(max(0,boundary-time.time()))
        self.targets={t.spec.id:t for t in allowed};self.offset=(await asyncio.to_thread(self.gate.load_cursor))['offset']
    async def update(self,targets):
        await self.prepare(targets);await asyncio.to_thread(self.gate.mark_ready,list(self.targets.values()))
    async def close(self):self.stopped=True
    async def poll(self,*,timeout=20):
        from app.services.channel_connection_telegram import TelegramApiRejected
        if self.stopped or not self.targets:raise RuntimeError('TELEGRAM_LISTENER_STOPPED')
        target=next(iter(self.targets.values()));self.offset=(await asyncio.to_thread(self.gate.load_cursor))['offset']
        try:
            value=await asyncio.to_thread(self.adapter.http.call,'getUpdates',target.transport.credentials['bot_token'],{'offset':self.offset,'limit':100,'timeout':timeout,'allowed_updates':['message','callback_query']})
        except TelegramApiRejected as error:
            self.adapter.not_before[self.bot_id]=time.monotonic()+max(5,error.retry_after or 5);raise
        if not isinstance(value,list) or len(value)>100:raise HTTPException(503,'TELEGRAM_POLL_INVALID')
        return value
    async def listen(self,accept):
        while not self.stopped:
            batch=self.buffer;self.buffer=[]
            if not batch:batch=await self.poll()
            if not batch:await asyncio.sleep(.05)
            for update in batch:
                if self.stopped:return
                await self.handle(update,accept)
    async def handle(self,update,accept):
        if self.stopped:raise HTTPException(409,'TELEGRAM_LISTENER_STOPPED')
        identifier=update.get('update_id') if isinstance(update,dict) else None
        if type(identifier) is not int or not 0<=identifier<9007199254740991:raise HTTPException(422,'TELEGRAM_UPDATE_INVALID')
        if identifier<self.offset:return
        try:await self.dispatch(update,accept)
        except NativePacketRejected:pass
        except HTTPException as error:
            definite=error.status_code==422 and error.detail=='TELEGRAM_EVENT_TIME_INVALID'
            disconnected=error.status_code in {404,409} and error.detail in {'CONNECTION_CHANGED','CONNECTION_TARGET_INACTIVE','CONNECTION_NOT_FOUND'}
            if not definite and not disconnected:raise
        await asyncio.to_thread(self.gate.advance,identifier);self.offset=identifier+1
    async def dispatch(self,update,accept):
        query=update.get('callback_query');message=query.get('message') if isinstance(query,dict) else update.get('message')
        if not isinstance(message,dict):return
        chat=message.get('chat')
        if not isinstance(chat,dict):return
        targets=[t for t in self.targets.values() if t.transport.chat_id==str(chat.get('id'))]
        if len(targets)>1:raise HTTPException(409,'TELEGRAM_TARGET_AMBIGUOUS')
        if not targets:return
        target=targets[0];binding=target.transport
        normalized=await asyncio.to_thread(convert_channel_packet,'telegram','normalize',{'input':update,'connection':{'botUserId':binding.bot_user_id,'botUsername':binding.bot_username,'chatId':binding.chat_id,'chatType':binding.chat_type}})
        if normalized is None:return
        try:
            stamp=message.get('date')
            if type(stamp) is not int or stamp<1:raise ValueError()
            normalized['occurredAt']=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        except (ValueError,TypeError,OverflowError):raise HTTPException(422,'TELEGRAM_EVENT_TIME_INVALID') from None
        context={'bot_id':self.bot_id,'chat_id':binding.chat_id,'thread_id':normalized.get('threadId'),'generation':target.spec.generation}
        if isinstance(query,dict):
            callback=query.get('id')
            if not isinstance(callback,str) or not 0<len(callback)<=200:raise HTTPException(422,'TELEGRAM_EVENT_TIME_INVALID')
            context.update(callback_id=callback,telegram_ack={'status':'pending','expires_at':time.time()+10})
        committed=await accept([InboundEnvelope(target.spec.id,normalized,context)])
        if not isinstance(committed,list) or len(committed)!=1:raise RuntimeError('TELEGRAM_COMMIT_UNCONFIRMED')
        if isinstance(query,dict):
            reservation=await asyncio.to_thread(self.gate.reserve_ack,committed[0])
            if reservation is not None:
                from app.services.channel_connection_telegram import TelegramApiRejected
                outcome='unknown'
                try:
                    result=await asyncio.wait_for(asyncio.to_thread(self.adapter.http.call,'answerCallbackQuery',binding.credentials['bot_token'],{'callback_query_id':reservation.callback_id}),2)
                    if result is True:outcome='confirmed'
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.to_thread(self.gate.finish_ack,committed[0],reservation.claim_id,'unknown'));raise
                except TelegramApiRejected:outcome='rejected'
                except Exception:pass
                await asyncio.to_thread(self.gate.finish_ack,committed[0],reservation.claim_id,outcome)
    def assert_live(self,target,did):
        with self.adapter.sessions() as db:
            row=state._live(db,target.spec.id,self.gate.lease_id,self.adapter.owner,self.gate.fence);item=state._delivery(db,did)
            if row.generation!=target.spec.generation or item.connection_id!=row.id or item.generation!=row.generation or item.status!='sending' or item.installation_fence!=self.gate.fence:raise HTTPException(409,'CONNECTION_CHANGED')
            db.commit()
    async def send(self,target,did,packets,context):
        from app.services.channel_connection_telegram import TelegramApiRejected,_id
        try:
            binding=target.transport
            if self.stopped or self.targets.get(target.spec.id)!=target or (context.get('bot_id'),context.get('chat_id'),context.get('generation'))!=(self.bot_id,binding.chat_id,target.spec.generation):raise ValueError()
            if not isinstance(packets,list) or len(packets)>20:raise ValueError()
            for packet in packets:
                if not isinstance(packet,dict) or set(packet)!={'method','parameters'} or packet['method'] not in {'sendMessage','sendPhoto'}:raise ValueError()
                p=packet['parameters'];fields={'chat_id','message_thread_id','text','parse_mode','reply_markup'} if packet['method']=='sendMessage' else {'chat_id','message_thread_id','photo','caption','parse_mode','reply_markup'}
                if not isinstance(p,dict) or set(p)-fields or p.get('chat_id')!=binding.chat_id or p.get('message_thread_id')!=context.get('thread_id') or len(json.dumps(packet,ensure_ascii=False).encode())>1000000:raise ValueError()
                if packet['method']=='sendPhoto' and not binding.can_send_photos:raise ValueError()
        except (ValueError,TypeError,KeyError):raise NativeNotSubmitted() from None
        receipts=[]
        for packet in packets:
            try:
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
                await asyncio.to_thread(self.assert_live,target,did)
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
            except (HTTPException,ValueError):
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('TELEGRAM_SEND_REVOKED') from None
            try:result=await asyncio.to_thread(self.adapter.http.call,packet['method'],binding.credentials['bot_token'],packet['parameters'])
            except TelegramApiRejected:
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('TELEGRAM_PARTIAL_DELIVERY') from None
            if not isinstance(result,dict) or not isinstance(result.get('chat'),dict) or _id(result['chat'].get('id'))!=binding.chat_id or result.get('message_thread_id')!=context.get('thread_id'):raise RuntimeError('TELEGRAM_RECEIPT_UNCONFIRMED')
            receipts.append(binding.chat_id+':'+_id(result.get('message_id'),positive=True))
        return receipts
