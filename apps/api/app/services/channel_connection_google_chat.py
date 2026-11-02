"""Google Chat 고정 API·서비스계정·공간 신원. 기존 방/큐 연결부가 사용한다."""
from __future__ import annotations
import asyncio,hashlib,json,re,time
from dataclasses import dataclass,field
from datetime import datetime,timezone
from urllib.parse import urlencode
from urllib.request import Request,build_opener
from urllib.error import HTTPError
from uuid import UUID,uuid5,NAMESPACE_URL
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import HTTPException
from app.services.channel_connection_google_chat_auth import GoogleChatAuthenticator,_NoRedirect

OAUTH_URL='https://oauth2.googleapis.com/token'
CHAT_ROOT='https://chat.googleapis.com/v1/'


def resource_name(value,kind,space=None):
    segment='[A-Za-z0-9_.~-]+'
    pattern='spaces/'+segment if kind=='space' else 'users/'+segment if kind=='user' else 'spaces/'+segment+'/'+kind+'s/'+segment
    if not isinstance(value,str) or not re.fullmatch(pattern,value) or any(v in {'.','..'} for v in value.split('/')) or space is not None and not value.startswith(space+'/'):raise HTTPException(422,'GOOGLE_CHAT_RESOURCE_INVALID')
    return value


@dataclass(frozen=True)
class ServiceAccount:
    project_id:str
    client_email:str=field(repr=False)
    key_id:str=field(repr=False)
    private_key:object=field(repr=False)


def service_account(credentials):
    try:
        raw=credentials.get('service_account_json')
        if not isinstance(raw,str) or not 0<len(raw)<=24000:raise ValueError()
        value=json.loads(raw);project=value['project_id'];email=value['client_email'];keyid=value['private_key_id']
        if value.get('type')!='service_account' or not isinstance(project,str) or not re.fullmatch('[a-z][a-z0-9-]{4,61}[a-z0-9]',project):raise ValueError()
        if credentials.get('project_id',project)!=project or value.get('token_uri')!=OAUTH_URL:raise ValueError()
        if not isinstance(email,str) or not re.fullmatch('[a-zA-Z0-9_-]+@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',email):raise ValueError()
        if not isinstance(keyid,str) or not re.fullmatch('[A-Za-z0-9_-]{1,200}',keyid):raise ValueError()
        key=serialization.load_pem_private_key(value['private_key'].encode(),password=None)
        if not isinstance(key,RSAPrivateKey) or key.key_size<2048:raise ValueError()
        return ServiceAccount(project,email,keyid,key)
    except (ValueError,TypeError,AttributeError,KeyError):raise HTTPException(422,'GOOGLE_CHAT_CREDENTIALS_INVALID') from None


class GoogleChatApiRejected(Exception):
    def __init__(self,status):self.status=status;super().__init__('GOOGLE_CHAT_API_REJECTED')


def _request(method,url,body,token):
    try:
        headers={'Accept':'application/json'}
        if token is not None:headers['Authorization']='Bearer '+token
        if body is not None:headers['Content-Type']='application/x-www-form-urlencoded' if url==OAUTH_URL else 'application/json'
        try:response=build_opener(_NoRedirect()).open(Request(url,data=body,headers=headers,method=method),timeout=5)
        except HTTPError as error:response=error
        with response:
            raw=response.read(1048577)
            if len(raw)>1048576:raise ValueError()
            if response.status in {400,401,403,404,409,429}:raise GoogleChatApiRejected(response.status)
            if not 200<=response.status<300:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except GoogleChatApiRejected:raise
    except Exception:raise HTTPException(503,'GOOGLE_CHAT_TRANSPORT_FAILED') from None


class GoogleChatHttpTransport:
    def __init__(self,*,request=_request):self.request=request
    def oauth(self,identity):
        now=int(time.time());assertion=jwt.encode({'iss':identity.client_email,'scope':'https://www.googleapis.com/auth/chat.bot','aud':OAUTH_URL,'iat':now,'exp':now+3600},identity.private_key,algorithm='RS256',headers={'kid':identity.key_id})
        body=urlencode({'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion}).encode()
        return self.request('POST',OAUTH_URL,body,None)
    def get(self,path,token):
        if path.endswith('/members/app'):resource_name(path[:-len('/members/app')],'space')
        else:resource_name(path,'space')
        return self.request('GET',CHAT_ROOT+path,None,token)
    def send(self,space,token,body,request_id,reply_option):
        resource_name(space,'space')
        try:
            if str(UUID(request_id))!=request_id or not isinstance(body,dict) or set(body)-{'text','cardsV2','thread'}:raise ValueError()
            if 'thread' in body:resource_name(body['thread']['name'],'thread',space)
            raw=json.dumps(body,ensure_ascii=False).encode()
            if len(raw)>32000 or reply_option not in {None,'REPLY_MESSAGE_OR_FAIL'}:raise ValueError()
        except (ValueError,TypeError,KeyError):raise HTTPException(422,'GOOGLE_CHAT_PACKET_INVALID') from None
        query={'requestId':request_id}
        if reply_option:query['messageReplyOption']=reply_option
        return self.request('POST',CHAT_ROOT+space+'/messages?'+urlencode(query),raw,token)


def verify_space(http,token,space,bot):
    resource_name(space,'space');resource_name(bot,'user')
    try:
        value=http.get(space,token);member=http.get(space+'/members/app',token)
        if value.get('name')!=space or value.get('spaceType') not in {'DIRECT_MESSAGE','SPACE','GROUP_CHAT'}:raise ValueError()
        if member.get('state')!='JOINED' or member.get('member',{}).get('type')!='BOT' or member['member'].get('name')!=bot:raise ValueError()
        if not isinstance(member.get('name'),str) or not member['name'].startswith(space+'/members/'):raise ValueError()
        return value['spaceType']
    except (ValueError,TypeError,KeyError,AttributeError,GoogleChatApiRejected):raise HTTPException(403,'GOOGLE_CHAT_TARGET_VERIFICATION_FAILED') from None


from sqlalchemy import select
from app.core.config import settings
from app.models import ChannelConnection,ChannelInstallationLease,ChannelConnectionDelivery
from app.services import channel_connection_state as state
from app.services.channel_connections import encrypt_connection_state,decrypt_connection_state
from app.services.channel_connection_supervisor import VerifiedTarget,NativeHandshakePending,NativeNotSubmitted
from app.services.channel_connection_bridge import convert_channel_packet,NativePacketRejected


@dataclass(frozen=True)
class GoogleChatBinding:
    credentials:dict=field(repr=False)
    space_type:str


class GoogleChatAdapter:
    def __init__(self,sessions,*,owner,http=None,authenticator=None):
        self.sessions=sessions;self.owner=owner;self.http=http or GoogleChatHttpTransport();self.authenticator=authenticator or GoogleChatAuthenticator();self.tokens={};self.token_lock=asyncio.Lock()
    @staticmethod
    def scope(project):
        if not isinstance(project,str) or not re.fullmatch('[1-9][0-9]{0,19}',project):raise HTTPException(422,'GOOGLE_CHAT_CONFIGURATION_INVALID')
        return 'google-chat-project:'+project
    async def _token(self,credentials):
        identity=service_account(credentials);key=hashlib.sha256(json.dumps(credentials,sort_keys=True).encode()).hexdigest()
        async with self.token_lock:
            cached=self.tokens.get(key)
            if cached and cached[1]>time.monotonic():return cached[0]
            value=await asyncio.to_thread(self.http.oauth,identity);token=value.get('access_token');expiry=value.get('expires_in')
            if not isinstance(token,str) or not token or len(token)>32768 or any(c.isspace() for c in token) or value.get('token_type','').lower()!='bearer' or type(expiry) is not int or not 60<expiry<=86400:raise HTTPException(503,'GOOGLE_CHAT_ACCESS_TOKEN_INVALID')
            self.tokens[key]=(token,time.monotonic()+min(expiry,3600)-60);return token
    @staticmethod
    def _binding(spec,space_type):return {'generation':spec.generation,'project':spec.installation_id,'bot':spec.account_id,'space':spec.target_id,'space_type':space_type}
    def _prepare(self,spec,space_type):
        with self.sessions() as db:
            row=state._connection(db,spec.id,lock=False)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row)
            lease=state.claim_installation(db,row,self.owner,ttl_seconds=45,listener_scope=self.scope(spec.installation_id),exclusive=True)
            if lease is None:raise HTTPException(409,'GOOGLE_CHAT_APP_SCOPE_IN_USE')
            row=state._connection(db,spec.id)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row);cursor=dict(lease.cursor_json or {});bindings=dict(cursor.get('google_chat_bindings',{}));binding=self._binding(spec,space_type)
            bindings[str(row.id)]=binding;cursor['google_chat_bindings']=bindings;lease.cursor_json=cursor
            row.lease_owner=self.owner;row.lease_expires_at=lease.lease_expires_at
            if row.accept_after is None:row.accept_after=state.clock(db)
            saved=cursor.get('google_chat_proofs',{}).get(str(row.id));ready=False
            if isinstance(saved,dict) and saved.get('generation')==spec.generation:
                proof=decrypt_connection_state(saved['ciphertext'],row.organization_id,row.id,settings.channel_connection_encryption_key,saved['key_version'],'google-chat-handshake:'+str(spec.generation))
                if proof!=binding:raise HTTPException(409,'GOOGLE_CHAT_PROOF_SCOPE_MISMATCH')
                ready=True
            db.commit();return ready
    async def verify(self,spec,credentials):
        if spec.provider!='google_chat':raise HTTPException(422,'GOOGLE_CHAT_CONFIGURATION_INVALID')
        scope=self.scope(spec.installation_id);resource_name(spec.account_id,'user');resource_name(spec.target_id,'space')
        token=await self._token(credentials);kind=await asyncio.to_thread(verify_space,self.http,token,spec.target_id,spec.account_id)
        if not await asyncio.to_thread(self._prepare,spec,kind):raise NativeHandshakePending()
        return VerifiedTarget(spec,spec.installation_id,spec.account_id,scope,GoogleChatBinding(dict(credentials),kind),True)
    async def open(self,targets):return GoogleChatListener(self,targets)
    def _candidate(self,project):
        with self.sessions() as db:return db.scalar(select(ChannelConnection.id).where(ChannelConnection.provider=='google_chat',ChannelConnection.installation_id==project,ChannelConnection.desired_state=='connected',ChannelConnection.deleted_at.is_(None)).limit(1))
    def _lookup(self,project,space):
        resource_name(space,'space')
        with self.sessions() as db:
            rows=list(db.scalars(select(ChannelConnection).where(ChannelConnection.provider=='google_chat',ChannelConnection.installation_id==project,ChannelConnection.target_id==space,ChannelConnection.desired_state=='connected',ChannelConnection.deleted_at.is_(None))))
            scope=state.scoped_hash('verified-listener:google_chat:'+self.scope(project))
            leases=list(db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.provider=='google_chat',ChannelInstallationLease.listener_scope_hash==scope,ChannelInstallationLease.owner.is_not(None),ChannelInstallationLease.lease_expires_at>state.clock(db))))
            if len(leases)!=1:raise HTTPException(503,'GOOGLE_CHAT_RECEIVER_NOT_READY')
            lease=leases[0];bindings=(lease.cursor_json or {}).get('google_chat_bindings',{});selected=[]
            for row in rows:
                binding=bindings.get(str(row.id))
                if row.organization_id==lease.organization_id and isinstance(binding,dict) and binding=={'generation':row.generation,'project':project,'bot':row.account_id,'space':space,'space_type':binding.get('space_type')}:selected.append((row.id,row.generation,lease.id,dict(binding)))
            if len(selected)!=1:raise HTTPException(403,'GOOGLE_CHAT_TARGET_FORBIDDEN')
            return selected[0]
    def _accept(self,selected,event):
        cid,generation,lid,binding=selected
        with self.sessions() as db:
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.id==lid).with_for_update())
            if lease is None or not lease.owner or lease.lease_expires_at is None or lease.lease_expires_at<=state.clock(db):raise HTTPException(503,'GOOGLE_CHAT_RECEIVER_NOT_READY')
            row=state._connection(db,cid)
            if (row.organization_id,row.provider,state._listener_scope(row,self.scope(binding['project'])))!=(lease.organization_id,lease.provider,lease.listener_scope_hash):raise HTTPException(409,'INSTALLATION_SCOPE_MISMATCH')
            state._target_active(db,row)
            if row.generation!=generation or row.installation_id!=binding['project'] or row.account_id!=binding['bot'] or row.target_id!=binding['space']:raise HTTPException(409,'CONNECTION_CHANGED')
            cursor=dict(lease.cursor_json or {})
            if cursor.get('google_chat_bindings',{}).get(str(cid))!=binding:raise HTTPException(409,'CONNECTION_CHANGED')
            proofs=dict(cursor.get('google_chat_proofs',{}));proofs[str(cid)]={'generation':generation,'ciphertext':encrypt_connection_state(binding,row.organization_id,cid,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'google-chat-handshake:'+str(generation)),'key_version':settings.channel_connection_key_version}
            cursor['google_chat_proofs']=proofs;lease.cursor_json=cursor
            if row.observed_state!='connected':db.commit();return None
            existing=db.execute(select(ChannelConnectionDelivery.id,ChannelConnectionDelivery.request_hash).where(ChannelConnectionDelivery.connection_id==cid,ChannelConnectionDelivery.event_key==state.scoped_hash(event['eventId']))).one_or_none()
            if existing is not None:
                # 결과 저장은 delivery를 잠근다. 중복 접수는 불변 ID/hash만 읽어 잠금 순환을 피한다.
                if existing.request_hash!=state._event(event):raise HTTPException(409,'NATIVE_EVENT_CONFLICT')
                db.commit();return {'delivery_id':str(existing.id)}
            item=state.accept_delivery(db,cid,lid,lease.owner,lease.fence,event)
            if existing is None and item.status=='received' and event['addressed']:
                busy=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==cid,ChannelConnectionDelivery.id!=item.id,ChannelConnectionDelivery.generation==generation,ChannelConnectionDelivery.native_room_hash==item.native_room_hash,ChannelConnectionDelivery.status.in_(['received','executing','ready','sending','unknown']),ChannelConnectionDelivery.event_json['addressed'].astext=='true').limit(1))
                if busy is not None:item.status='failed';item.last_error_code='ROOM_BUSY'
            if item.context_ciphertext is None:
                context={**binding,'thread':event.get('threadName')}
                item.context_ciphertext=encrypt_connection_state(context,row.organization_id,cid,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'delivery:'+str(item.id));item.context_key_version=settings.channel_connection_key_version
            result={'delivery_id':str(item.id)};db.commit();return result
    async def receive(self,authorization,body):
        try:
            parts=authorization.split(' ')
            if len(parts)!=2 or parts[0].lower()!='bearer' or len(authorization)>20000:raise ValueError()
            project=jwt.decode(parts[1],options={'verify_signature':False})['aud'];self.scope(project)
        except (jwt.PyJWTError,ValueError,TypeError,KeyError,HTTPException,AttributeError):raise HTTPException(401,'GOOGLE_CHAT_AUTHENTICATION_FAILED') from None
        if not await asyncio.to_thread(self._candidate,project):raise HTTPException(401,'GOOGLE_CHAT_AUTHENTICATION_FAILED')
        await asyncio.to_thread(self.authenticator.authenticate,authorization,project)
        if body.get('type') not in {'MESSAGE','CARD_CLICKED'}:return {'ignored':True}
        space=body.get('space')
        if not isinstance(space,dict):raise HTTPException(422,'GOOGLE_CHAT_EVENT_INVALID')
        selected=await asyncio.to_thread(self._lookup,project,space.get('name'));binding=selected[3]
        try:event=await asyncio.to_thread(convert_channel_packet,'google-chat','normalize',{'input':body,'connection':{'botUserName':binding['bot'],'spaceName':binding['space'],'spaceType':binding['space_type']}})
        except NativePacketRejected:raise HTTPException(422,'GOOGLE_CHAT_EVENT_INVALID') from None
        if event is None:return {'ignored':True}
        try:
            stamp=datetime.fromisoformat(body['eventTime'])
            if stamp.tzinfo is None:raise ValueError()
        except (ValueError,TypeError,KeyError):raise HTTPException(422,'GOOGLE_CHAT_EVENT_TIME_INVALID') from None
        event['occurredAt']=stamp.astimezone(timezone.utc).isoformat();state._event(event)
        if not event['addressed']:
            # 첫 연결 신원은 봇 호출/카드 선택으로만 확정한다. 일반 대화는 실행하지 않는다.
            return {'ignored':True}
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=await asyncio.to_thread(self._accept,selected,event)
            if result is not None:return result
            await asyncio.sleep(.05)
        raise HTTPException(503,'GOOGLE_CHAT_RECEIVER_NOT_READY')


class GoogleChatListener:
    def __init__(self,adapter,targets):self.adapter=adapter;self.targets={t.spec.id:t for t in targets};self.stopped=asyncio.Event()
    async def update(self,targets):self.targets={t.spec.id:t for t in targets}
    async def listen(self,accept):await self.stopped.wait()
    async def close(self):self.stopped.set()
    def _assert_live(self,target,did):
        with self.adapter.sessions() as db:
            scope=state.scoped_hash('verified-listener:google_chat:'+target.listener_scope)
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==target.spec.organization_id,ChannelInstallationLease.provider=='google_chat',ChannelInstallationLease.listener_scope_hash==scope).with_for_update())
            if lease is None:raise HTTPException(409,'INSTALLATION_LEASE_LOST')
            row=state._live(db,target.spec.id,lease.id,self.adapter.owner,lease.fence);item=state._delivery(db,did)
            if row.generation!=target.spec.generation or item.connection_id!=row.id or item.generation!=row.generation or item.status!='sending' or item.installation_fence!=lease.fence:raise HTTPException(409,'CONNECTION_CHANGED')
            db.commit()
    async def send(self,target,did,packets,context):
        try:
            if self.stopped.is_set() or self.targets.get(target.spec.id)!=target or {k:context.get(k) for k in ['generation','project','bot','space','space_type']}!=self.adapter._binding(target.spec,target.transport.space_type):raise ValueError()
            if not isinstance(packets,list) or len(packets)>20:raise ValueError()
            for packet in packets:
                if not isinstance(packet,dict) or set(packet)-{'parent','body','messageReplyOption'} or packet['parent']!=target.spec.target_id:raise ValueError()
                body=packet['body'];thread=body.get('thread',{}).get('name')
                if thread!=context.get('thread') or packet.get('messageReplyOption')!=('REPLY_MESSAGE_OR_FAIL' if thread else None) or len(json.dumps(body,ensure_ascii=False).encode())>32000:raise ValueError()
            token=await self.adapter._token(target.transport.credentials)
        except (HTTPException,ValueError,TypeError,KeyError,AttributeError):raise NativeNotSubmitted() from None
        receipts=[]
        for index,packet in enumerate(packets):
            try:
                if self.stopped.is_set() or self.targets.get(target.spec.id)!=target:raise ValueError()
                await asyncio.to_thread(self._assert_live,target,did)
                if self.stopped.is_set() or self.targets.get(target.spec.id)!=target:raise ValueError()
            except (HTTPException,ValueError):
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('GOOGLE_CHAT_SEND_REVOKED') from None
            try:result=await asyncio.to_thread(self.adapter.http.send,target.spec.target_id,token,packet['body'],str(uuid5(NAMESPACE_URL,'cga-google-chat:'+str(did)+':'+str(index))),packet.get('messageReplyOption'))
            except GoogleChatApiRejected:
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('GOOGLE_CHAT_PARTIAL_SEND_REJECTED') from None
            receipts.append(resource_name(result.get('name'),'message',target.spec.target_id))
        return receipts
