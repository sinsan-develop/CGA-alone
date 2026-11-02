"""LINE 고정 API·서명 수신과 기존 방/큐/전달 원장 연결."""
from __future__ import annotations
import asyncio,json,re,time
from dataclasses import dataclass,field
from datetime import datetime,timezone
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import object_session
from app.core.config import settings
from app.models import ChannelConnection,ChannelInstallationLease,ChannelConnectionDelivery
from app.services import channel_connection_state as state
from app.services.channel_connections import encrypt_connection_state,decrypt_connection_state,decrypt_credentials
from app.services.channel_connection_supervisor import VerifiedTarget,NativeHandshakePending,NativeNotSubmitted
from app.services.channel_connection_bridge import convert_channel_packet,NativePacketRejected
from app.services.channel_connection_line_auth import verify_line_signature
from app.services.channel_connection_google_chat_auth import _NoRedirect

ROOT='https://api.line.me/v2/bot/'

def source_type(value):
    if not isinstance(value,str) or not re.fullmatch('[UCR][a-f0-9]{32}',value):raise HTTPException(422,'LINE_SOURCE_INVALID')
    return {'U':'user','C':'group','R':'room'}[value[0]]

def credentials(value):
    token=value.get('access_token');secret=value.get('channel_secret')
    if not isinstance(token,str) or not token or len(token)>8192 or any(c.isspace() for c in token) or not isinstance(secret,str) or not secret.strip() or len(secret)>4096:raise HTTPException(422,'LINE_CREDENTIALS_INVALID')
    return token,secret

class LineApiRejected(Exception):pass

def _request(method,url,body,token):
    try:
        headers={'Accept':'application/json','Authorization':'Bearer '+token}
        if body is not None:headers['Content-Type']='application/json'
        try:response=build_opener(_NoRedirect()).open(Request(url,data=body,headers=headers,method=method),timeout=5)
        except HTTPError as error:response=error
        with response:
            raw=response.read(1048577)
            if len(raw)>1048576:raise ValueError()
            if response.status in {400,401,403,404,409,429}:raise LineApiRejected()
            if not 200<=response.status<300:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except LineApiRejected:raise
    except Exception:raise HTTPException(503,'LINE_TRANSPORT_FAILED') from None

class LineHttpTransport:
    def __init__(self,*,request=_request):self.request=request
    def bot_info(self,token):return self.request('GET',ROOT+'info',None,token)
    def reply(self,token,body):
        try:
            if not isinstance(body,dict) or set(body)!={'replyToken','messages'} or not isinstance(body['replyToken'],str) or not 0<len(body['replyToken'])<=1024 or not isinstance(body['messages'],list) or not 1<=len(body['messages'])<=5:raise ValueError()
            data=json.dumps(body,ensure_ascii=False).encode()
            if len(data)>262144:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(422,'LINE_REPLY_INVALID') from None
        return self.request('POST',ROOT+'message/reply',data,token)

@dataclass(frozen=True)
class LineBinding:
    credentials:dict=field(repr=False)
    source_type:str

class LineAdapter:
    def __init__(self,sessions,*,owner,http=None):self.sessions=sessions;self.owner=owner;self.http=http or LineHttpTransport()
    @staticmethod
    def scope(bot):
        if source_type(bot)!='user':raise HTTPException(422,'LINE_BOT_INVALID')
        return 'line-bot:'+bot
    @staticmethod
    def _binding(spec,kind):return {'generation':spec.generation,'bot':spec.account_id,'source':spec.target_id,'source_type':kind}
    def _prepare(self,spec,kind):
        with self.sessions() as db:
            row=state._connection(db,spec.id,lock=False)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row)
            lease=state.claim_installation(db,row,self.owner,ttl_seconds=45,listener_scope=self.scope(spec.account_id),exclusive=True)
            if lease is None:raise HTTPException(409,'LINE_APP_SCOPE_IN_USE')
            row=state._connection(db,spec.id)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row);cursor=dict(lease.cursor_json or {});bindings=dict(cursor.get('line_bindings',{}));binding=self._binding(spec,kind)
            bindings[str(row.id)]=binding;cursor['line_bindings']=bindings;lease.cursor_json=cursor
            row.lease_owner=self.owner;row.lease_expires_at=lease.lease_expires_at
            if row.accept_after is None:row.accept_after=state.clock(db)
            saved=cursor.get('line_proofs',{}).get(str(row.id));ready=False
            if isinstance(saved,dict) and saved.get('generation')==spec.generation:
                proof=decrypt_connection_state(saved['ciphertext'],row.organization_id,row.id,settings.channel_connection_encryption_key,saved['key_version'],'line-handshake:'+str(spec.generation))
                if proof!=binding:raise HTTPException(409,'LINE_PROOF_SCOPE_MISMATCH')
                ready=True
            if ready and row.observed_state!='connected':row.observed_state='connecting'
            db.commit();return ready
    async def verify(self,spec,value):
        token,_=credentials(value);scope=self.scope(spec.account_id);kind=source_type(spec.target_id)
        if spec.provider!='line' or spec.installation_id!=spec.account_id:raise HTTPException(422,'LINE_CONFIGURATION_INVALID')
        try:
            info=await asyncio.to_thread(self.http.bot_info,token)
            if info.get('userId')!=spec.account_id:raise ValueError()
        except (LineApiRejected,ValueError,TypeError,AttributeError):raise HTTPException(403,'LINE_BOT_VERIFICATION_FAILED') from None
        if not await asyncio.to_thread(self._prepare,spec,kind):raise NativeHandshakePending()
        return VerifiedTarget(spec,spec.installation_id,spec.account_id,scope,LineBinding(dict(value),kind),True)
    async def open(self,targets):return LineListener(self,targets)
    def _candidates(self):
        with self.sessions() as db:
            leases=list(db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.provider=='line',ChannelInstallationLease.owner.is_not(None),ChannelInstallationLease.lease_expires_at>state.clock(db))))
            result=[]
            for lease in leases:
                for cid,binding in (lease.cursor_json or {}).get('line_bindings',{}).items():
                    row=db.get(ChannelConnection,cid)
                    if row is None or row.deleted_at is not None or row.desired_state!='connected' or row.organization_id!=lease.organization_id or row.provider!='line' or row.installation_id!=row.account_id:continue
                    if not isinstance(binding,dict) or binding!=self._binding(row,source_type(row.target_id)) or lease.listener_scope_hash!=state.scoped_hash('verified-listener:line:'+self.scope(row.account_id)):continue
                    value=decrypt_credentials(row.credential_ciphertext,row.organization_id,row.id,settings.channel_connection_encryption_key,row.credential_key_version)
                    result.append((row.id,row.generation,lease.id,dict(binding),value))
            return result
    def _accept(self,selected,event,reply_token,received_at):
        cid,generation,lid,binding,_=selected
        with self.sessions() as db:
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.id==lid).with_for_update())
            if lease is None or not lease.owner or lease.lease_expires_at is None or lease.lease_expires_at<=state.clock(db):raise HTTPException(503,'LINE_RECEIVER_NOT_READY')
            row=state._connection(db,cid)
            if (row.organization_id,row.provider,state._listener_scope(row,self.scope(binding['bot'])))!=(lease.organization_id,lease.provider,lease.listener_scope_hash):raise HTTPException(409,'INSTALLATION_SCOPE_MISMATCH')
            state._target_active(db,row)
            if row.generation!=generation or row.account_id!=binding['bot'] or row.target_id!=binding['source']:raise HTTPException(409,'CONNECTION_CHANGED')
            cursor=dict(lease.cursor_json or {})
            if cursor.get('line_bindings',{}).get(str(cid))!=binding:raise HTTPException(409,'CONNECTION_CHANGED')
            event_key=state.scoped_hash(event['eventId']);request_hash=state._event(event);token_hash=state.scoped_hash(reply_token)
            events=dict(cursor.get('line_events',{}));receipt=events.get(event_key)
            if receipt is not None and (receipt.get('connection')!=str(cid) or receipt.get('request_hash')!=request_hash or receipt.get('token_hash')!=token_hash):raise HTTPException(409,'NATIVE_EVENT_CONFLICT')
            existing=db.execute(select(ChannelConnectionDelivery.id,ChannelConnectionDelivery.request_hash,ChannelConnectionDelivery.context_ciphertext,ChannelConnectionDelivery.context_key_version).where(ChannelConnectionDelivery.connection_id==cid,ChannelConnectionDelivery.event_key==event_key)).one_or_none()
            if existing is not None:
                context=decrypt_connection_state(existing.context_ciphertext,row.organization_id,cid,settings.channel_connection_encryption_key,existing.context_key_version,'delivery:'+str(existing.id))
                if existing.request_hash!=request_hash or context.get('reply_hash')!=token_hash or receipt is not None and receipt.get('delivery')!=str(existing.id):raise HTTPException(409,'NATIVE_EVENT_CONFLICT')
                db.commit();return str(existing.id)
            if receipt is not None:raise HTTPException(409,'LINE_EVENT_RECEIPT_UNAVAILABLE')
            tokens=dict(cursor.get('line_reply_tokens',{}))
            if token_hash in tokens:raise HTTPException(409,'LINE_REPLY_TOKEN_REUSED')
            proofs=dict(cursor.get('line_proofs',{}));proofs[str(cid)]={'generation':generation,'ciphertext':encrypt_connection_state(binding,row.organization_id,cid,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'line-handshake:'+str(generation)),'key_version':settings.channel_connection_key_version};cursor['line_proofs']=proofs;lease.cursor_json=cursor
            if row.observed_state!='connected':db.commit();return None
            item=state.accept_delivery(db,cid,lid,lease.owner,lease.fence,event)
            tokens[token_hash]={'delivery':str(item.id),'connection':str(cid),'generation':generation};cursor['line_reply_tokens']=tokens
            events[event_key]={'delivery':str(item.id),'connection':str(cid),'request_hash':request_hash,'token_hash':token_hash};cursor['line_events']=events;lease.cursor_json=cursor
            if item.status=='received':
                busy=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==cid,ChannelConnectionDelivery.id!=item.id,ChannelConnectionDelivery.generation==generation,ChannelConnectionDelivery.native_room_hash==item.native_room_hash,ChannelConnectionDelivery.status.in_(['received','executing','ready','sending','unknown']),ChannelConnectionDelivery.event_json['addressed'].astext=='true').limit(1))
                if busy is not None:item.status='failed';item.last_error_code='ROOM_BUSY'
            context={**binding,'reply_token':reply_token,'reply_hash':state.scoped_hash(reply_token),'received_at':received_at}
            item.context_ciphertext=encrypt_connection_state(context,row.organization_id,cid,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'delivery:'+str(item.id));item.context_key_version=settings.channel_connection_key_version
            db.commit();return str(item.id)
    def _received_at(self):
        with self.sessions() as db:return state.clock(db).timestamp()
    async def receive(self,signature,raw):
        if not isinstance(raw,bytes) or len(raw)>262144:raise HTTPException(413,'LINE_EVENT_TOO_LARGE')
        received_at=await asyncio.to_thread(self._received_at);candidates=await asyncio.to_thread(self._candidates);authenticated=[]
        # JSON 파싱 전 서버에서 검증한 lease의 비밀값으로만 원문을 검증한다.
        for candidate in candidates:
            try:verify_line_signature(raw,signature,credentials(candidate[4])[1])
            except HTTPException:continue
            authenticated.append(candidate)
        if not authenticated:raise HTTPException(401,'LINE_AUTHENTICATION_FAILED')
        try:
            body=json.loads(raw)
            if not isinstance(body,dict) or not isinstance(body.get('events'),list) or len(body['events'])>100:raise ValueError()
        except (ValueError,TypeError,UnicodeError):raise HTTPException(422,'LINE_BODY_INVALID') from None
        matched=[c for c in authenticated if c[3]['bot']==body.get('destination')]
        if not matched:raise HTTPException(403,'LINE_TARGET_FORBIDDEN')
        deliveries=[]
        for index,native in enumerate(body['events']):
            if not isinstance(native,dict):raise HTTPException(422,'LINE_EVENT_INVALID')
            if native.get('mode')=='standby' or native.get('type') not in {'message','postback'}:continue
            source=native.get('source',{})
            if not isinstance(source,dict):raise HTTPException(422,'LINE_EVENT_INVALID')
            kind=source.get('type');sid=source.get('userId' if kind=='user' else 'groupId' if kind=='group' else 'roomId')
            choices=[c for c in matched if c[3]['source']==sid and c[3]['source_type']==kind]
            if len(choices)!=1:raise HTTPException(403,'LINE_TARGET_FORBIDDEN')
            selected=choices[0];binding=selected[3]
            if not source.get('userId'):continue
            try:event=await asyncio.to_thread(convert_channel_packet,'line','normalize',{'input':body,'eventIndex':index,'connection':{'botUserId':binding['bot'],'sourceType':kind,'sourceId':sid}})
            except NativePacketRejected:raise HTTPException(422,'LINE_EVENT_INVALID') from None
            if event is None or not event['addressed']:continue
            reply=native.get('replyToken')
            if not isinstance(reply,str) or not 0<len(reply)<=1024:raise HTTPException(422,'LINE_REPLY_TOKEN_INVALID')
            event.pop('replyToken',None);stamp=event.get('timestamp')
            if type(stamp) is not int or stamp<=0 or stamp>received_at*1000:raise HTTPException(422,'LINE_EVENT_TIME_INVALID')
            event['occurredAt']=datetime.fromtimestamp(stamp/1000,timezone.utc).isoformat();state._event(event)
            deadline=time.monotonic()+5
            while True:
                did=await asyncio.to_thread(self._accept,selected,event,reply,received_at)
                if did is not None:deliveries.append(did);break
                if time.monotonic()>=deadline:raise HTTPException(503,'LINE_RECEIVER_NOT_READY')
                await asyncio.sleep(.05)
        return {'deliveries':deliveries}

class LineListener:
    def __init__(self,adapter,targets):self.adapter=adapter;self.targets={t.spec.id:t for t in targets};self.stopped=asyncio.Event()
    async def update(self,targets):self.targets={t.spec.id:t for t in targets}
    async def listen(self,accept):await self.stopped.wait()
    async def close(self):self.stopped.set()
    @staticmethod
    def _window(context,event,now):
        received=context.get('received_at');stamp=event.get('timestamp')
        return type(received) in {int,float} and 0<=now-received<55 and (not event.get('redelivery') or type(stamp) is int and 0<=now*1000-stamp<1200000)
    def can_execute(self,item):
        db=object_session(item);row=db.get(ChannelConnection,item.connection_id)
        try:context=decrypt_connection_state(item.context_ciphertext,row.organization_id,row.id,settings.channel_connection_encryption_key,item.context_key_version,'delivery:'+str(item.id))
        except HTTPException:context={}
        valid=self._window(context,item.event_json,state.clock(db).timestamp())
        if not valid:item.status='failed';item.last_error_code='LINE_REPLY_EXPIRED';item.deadline_at=None
        return valid
    def _assert_live(self,target,did,context):
        with self.adapter.sessions() as db:
            scope=state.scoped_hash('verified-listener:line:'+target.listener_scope)
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==target.spec.organization_id,ChannelInstallationLease.provider=='line',ChannelInstallationLease.listener_scope_hash==scope).with_for_update())
            if lease is None:raise HTTPException(409,'INSTALLATION_LEASE_LOST')
            row=state._live(db,target.spec.id,lease.id,self.adapter.owner,lease.fence);item=state._delivery(db,did)
            if row.generation!=target.spec.generation or item.connection_id!=row.id or item.generation!=row.generation or item.status!='sending' or item.installation_fence!=lease.fence or not self._window(context,item.event_json,state.clock(db).timestamp()):raise HTTPException(409,'LINE_REPLY_NOT_AVAILABLE')
            reservation=(lease.cursor_json or {}).get('line_reply_tokens',{}).get(context.get('reply_hash'))
            if reservation!={'delivery':str(did),'connection':str(row.id),'generation':row.generation}:raise HTTPException(409,'LINE_REPLY_RESERVATION_LOST')
            db.commit()
    async def send(self,target,did,packets,context):
        try:
            if self.stopped.is_set() or self.targets.get(target.spec.id)!=target or {k:context.get(k) for k in ['generation','bot','source','source_type']}!=self.adapter._binding(target.spec,target.transport.source_type):raise ValueError()
            token,_=credentials(target.transport.credentials)
            if not isinstance(packets,list) or not 1<=len(packets)<=5:raise ValueError()
            body={'replyToken':context['reply_token'],'messages':packets}
            await asyncio.to_thread(self._assert_live,target,did,context)
            if self.stopped.is_set() or self.targets.get(target.spec.id)!=target:raise ValueError()
        except (HTTPException,ValueError,TypeError,KeyError):raise NativeNotSubmitted() from None
        try:result=await asyncio.to_thread(self.adapter.http.reply,token,body)
        except LineApiRejected:raise NativeNotSubmitted() from None
        messages=result.get('sentMessages')
        if not isinstance(messages,list) or len(messages)!=len(packets):raise RuntimeError('LINE_REPLY_RECEIPT_UNKNOWN')
        identifiers=[]
        for message in messages:
            identifier=message.get('id') if isinstance(message,dict) else None
            if type(identifier) is int and identifier>0:identifier=str(identifier)
            if not isinstance(identifier,str) or not re.fullmatch('[A-Za-z0-9_-]{1,200}',identifier):raise RuntimeError('LINE_REPLY_RECEIPT_UNKNOWN')
            identifiers.append(identifier)
        return identifiers
