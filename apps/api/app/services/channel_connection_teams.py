"""Teams native HTTP 수신/회신. 모델·대화는 기존 방/전달 작업자가 처리한다."""
from __future__ import annotations
import asyncio,hashlib,json,time
from dataclasses import dataclass,field
from datetime import datetime,timezone
from urllib.parse import urlencode,quote
from urllib.request import Request,build_opener
from uuid import UUID
import jwt
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.models import ChannelConnection,ChannelInstallationLease,ChannelConnectionDelivery
from app.services import channel_connection_state as state
from app.services.channel_connections import encrypt_connection_state,decrypt_connection_state
from app.services.channel_connection_supervisor import ConnectionSpec,VerifiedTarget,NativeHandshakePending,NativeNotSubmitted
from app.services.channel_connection_teams_auth import TeamsAuthenticator,validate_teams_service_url,_NoRedirect


def _uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(422,'TEAMS_CONFIGURATION_INVALID') from None


def _text(value,maximum=512):
    if not isinstance(value,str) or not value or len(value)>maximum:raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
    return value


def _post(url,headers,body):
    try:
        with build_opener(_NoRedirect()).open(Request(url,data=body,headers=headers,method='POST'),timeout=5) as response:
            raw=response.read(1048577)
            if not 200<=response.status<300 or len(raw)>1048576:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except Exception:raise HTTPException(503,'TEAMS_TRANSPORT_FAILED') from None


class TeamsHttpTransport:
    def __init__(self,*,post=_post):self.post=post
    def oauth(self,tenant,client,secret):
        tenant=_uuid(tenant);client=_uuid(client)
        if not isinstance(secret,str) or not secret or len(secret)>24000:raise HTTPException(422,'TEAMS_CONFIGURATION_INVALID')
        body=urlencode({'grant_type':'client_credentials','client_id':client,'client_secret':secret,'scope':'https://api.botframework.com/.default'}).encode()
        return self.post('https://login.microsoftonline.com/'+tenant+'/oauth2/v2.0/token',{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body)
    def send(self,service,conversation,activity,token,packet):
        service=validate_teams_service_url(service);conversation=_text(conversation,1800);activity=_text(activity)
        if not isinstance(token,str) or not token or len(token)>32768 or any(c.isspace() for c in token):raise HTTPException(503,'TEAMS_ACCESS_TOKEN_INVALID')
        raw=json.dumps(packet,ensure_ascii=False).encode()
        if len(raw)>28000:raise HTTPException(422,'TEAMS_PACKET_LIMIT')
        url=service+'v3/conversations/'+quote(conversation,safe='')+'/activities/'+quote(activity,safe='')
        return self.post(url,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'},raw)


@dataclass(frozen=True)
class TeamsBinding:
    credentials:dict=field(repr=False)
    service_url:str


class TeamsAdapter:
    def __init__(self,sessions,*,owner,http=None,authenticator=None):
        self.sessions=sessions;self.owner=owner;self.http=http or TeamsHttpTransport();self.authenticator=authenticator or TeamsAuthenticator();self.tokens={};self.token_lock=asyncio.Lock()

    @staticmethod
    def scope(client):return 'teams-app:'+_uuid(client)

    async def _token(self,credentials):
        tenant=_uuid(credentials.get('tenant_id'));client=_uuid(credentials.get('client_id'));secret=credentials.get('client_secret')
        key=hashlib.sha256(json.dumps(credentials,sort_keys=True).encode()).hexdigest()
        async with self.token_lock:
            cached=self.tokens.get(key)
            if cached and cached[1]>time.monotonic():return cached[0]
            value=await asyncio.to_thread(self.http.oauth,tenant,client,secret)
            token=value.get('access_token');expiry=value.get('expires_in')
            if not isinstance(token,str) or not token or len(token)>32768 or value.get('token_type','').lower()!='bearer' or type(expiry) is not int or not 60<expiry<=86400:raise HTTPException(503,'TEAMS_ACCESS_TOKEN_INVALID')
            self.tokens[key]=(token,time.monotonic()+min(expiry,3600)-60);return token

    def _prepare(self,spec):
        with self.sessions() as db:
            row=state._connection(db,spec.id,lock=False)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row)
            lease=state.claim_installation(db,row,self.owner,ttl_seconds=45,listener_scope=self.scope(spec.account_id),exclusive=True)
            if lease is None:raise HTTPException(409,'TEAMS_APP_SCOPE_IN_USE')
            row=state._connection(db,spec.id)
            if row.generation!=spec.generation:raise HTTPException(409,'CONNECTION_CHANGED')
            state._target_active(db,row)
            cursor=dict(lease.cursor_json or {});bindings=dict(cursor.get('teams_bindings',{}));proofs=dict(cursor.get('teams_proofs',{}))
            bindings[str(row.id)]={'generation':spec.generation,'tenant':spec.installation_id,'client':spec.account_id,'target':spec.target_id}
            cursor['teams_bindings']=bindings;lease.cursor_json=cursor
            row.lease_owner=self.owner;row.lease_expires_at=lease.lease_expires_at
            if row.accept_after is None:row.accept_after=state.clock(db)
            proof=proofs.get(str(row.id));value=None
            if isinstance(proof,dict) and proof.get('generation')==spec.generation:
                value=decrypt_connection_state(proof['ciphertext'],row.organization_id,row.id,settings.channel_connection_encryption_key,proof['key_version'],'teams-handshake:'+str(spec.generation))
                if (value.get('tenant'),value.get('client'),value.get('target'))!=(spec.installation_id,spec.account_id,spec.target_id):raise HTTPException(409,'TEAMS_PROOF_SCOPE_MISMATCH')
                validate_teams_service_url(value.get('service_url'))
            db.commit();return value

    async def verify(self,spec,credentials):
        if spec.provider!='ms_teams' or (_uuid(credentials.get('tenant_id')),_uuid(credentials.get('client_id')))!=(spec.installation_id,spec.account_id):raise HTTPException(422,'TEAMS_CONFIGURATION_INVALID')
        await self._token(credentials)
        proof=await asyncio.to_thread(self._prepare,spec)
        if proof is None:raise NativeHandshakePending()
        return VerifiedTarget(spec,spec.installation_id,spec.account_id,self.scope(spec.account_id),TeamsBinding(dict(credentials),proof['service_url']),True)

    async def open(self,targets):return TeamsListener(self,targets)

    def _client_candidate(self,client):
        with self.sessions() as db:
            return db.scalar(select(ChannelConnection.id).where(ChannelConnection.provider=='ms_teams',ChannelConnection.account_id==client,ChannelConnection.desired_state=='connected',ChannelConnection.deleted_at.is_(None)).limit(1))

    def _bind_activity(self,client,verified,activity):
        channel_data=activity.get('channelData')
        if not isinstance(channel_data,dict) or not isinstance(channel_data.get('tenant'),dict):raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
        tenant=_uuid(channel_data['tenant'].get('id'))
        conversation=activity.get('conversation');recipient=activity.get('recipient');sender=activity.get('from')
        if not all(isinstance(item,dict) for item in (conversation,recipient,sender)):raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
        conversation_id=_text(conversation.get('id'),1800);recipient_id=_text(recipient.get('id'));sender_id=_text(sender.get('id'))
        if recipient_id!='28:'+client:raise HTTPException(403,'TEAMS_RECIPIENT_FORBIDDEN')
        native_channel=channel_data.get('channel',{})
        if not isinstance(native_channel,dict):raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
        target=_text(native_channel.get('id') if isinstance(native_channel,dict) and native_channel.get('id') else conversation_id,200)
        with self.sessions() as db:
            candidates=db.scalars(select(ChannelConnection).where(ChannelConnection.provider=='ms_teams',ChannelConnection.account_id==client,ChannelConnection.installation_id==tenant,ChannelConnection.target_id==target,ChannelConnection.desired_state=='connected',ChannelConnection.deleted_at.is_(None))).all()
            if not candidates:raise HTTPException(403,'TEAMS_TARGET_FORBIDDEN')
            scope_hash=state._listener_scope(candidates[0],self.scope(client))
            leases=db.scalars(select(ChannelInstallationLease).where(ChannelInstallationLease.provider=='ms_teams',ChannelInstallationLease.listener_scope_hash==scope_hash,ChannelInstallationLease.lease_expires_at>state.clock(db),ChannelInstallationLease.owner.is_not(None)).with_for_update()).all()
            if len(leases)!=1:raise HTTPException(503,'TEAMS_RECEIVER_NOT_READY')
            lease=leases[0];cursor=dict(lease.cursor_json or {});bindings=cursor.get('teams_bindings',{})
            selected=[]
            for candidate in candidates:
                binding=bindings.get(str(candidate.id),{})
                if candidate.organization_id==lease.organization_id and binding=={'generation':candidate.generation,'tenant':tenant,'client':client,'target':target}:selected.append(candidate.id)
            if len(selected)!=1:raise HTTPException(403,'TEAMS_TARGET_FORBIDDEN')
            row=state._connection(db,selected[0]);state._target_active(db,row)
            generation=row.generation
            # 받은 JWT나 access token은 보관하지 않는다. 검증한 실제 목적지만 목적별 암호화한다.
            proof={'tenant':tenant,'client':client,'target':target,'service_url':verified.service_url}
            self._event(activity,proof,conversation_id,sender_id,recipient_id)
            proofs=dict(cursor.get('teams_proofs',{}));proofs[str(row.id)]={'generation':generation,'ciphertext':encrypt_connection_state(proof,row.organization_id,row.id,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'teams-handshake:'+str(generation)),'key_version':settings.channel_connection_key_version}
            cursor['teams_proofs']=proofs;lease.cursor_json=cursor;row.last_checked_at=state.clock(db)
            value=(row.id,generation,lease.id)
            db.commit();return value,proof,conversation_id,sender_id,recipient_id

    @staticmethod
    def _event(activity,proof,conversation_id,sender_id,recipient_id):
        try:
            stamp=datetime.fromisoformat(_text(activity.get('timestamp'),100))
            if stamp.tzinfo is None:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(422,'TEAMS_ACTIVITY_TIME_INVALID') from None
        event_id=_text(activity.get('id'));conversation=activity['conversation'];kind='bot' if activity['from'].get('role')=='bot' or sender_id.startswith('28:') else 'user'
        mode=conversation.get('conversationType')
        if mode not in {'personal','groupChat','channel'}:raise HTTPException(422,'TEAMS_CONVERSATION_TYPE_INVALID')
        text=activity.get('text') or '';addressed=mode=='personal'
        if not isinstance(text,str):raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
        entities=activity.get('entities') or []
        if not isinstance(entities,list):raise HTTPException(422,'TEAMS_ACTIVITY_INVALID')
        for entity in entities:
            if isinstance(entity,dict) and entity.get('type')=='mention' and isinstance(entity.get('mentioned'),dict) and entity['mentioned'].get('id')==recipient_id:
                mention=entity.get('text')
                if isinstance(mention,str) and mention and mention in text:addressed=True;text=text.replace(mention,'')
        if isinstance(activity.get('value'),dict):
            wire=activity['value']
            if wire.get('webchatRichFormVersion')!='1.0' or not wire.get('sourceMessageId') or not wire.get('sourceTalkNodeId'):raise HTTPException(422,'TEAMS_CARD_RESPONSE_INVALID')
            response=wire.get('response')
            if not isinstance(response,dict):raise HTTPException(422,'TEAMS_CARD_RESPONSE_INVALID')
            response=dict(response)
            for key,value in wire.items():
                if key not in {'webchatRichFormVersion','sourceMessageId','sourceTalkNodeId','response','__cga_buttonValue'}:response[key]=value
            if '__cga_buttonValue' in wire:response['buttonValue']=wire['__cga_buttonValue']
            standard={key:wire[key] for key in ('webchatRichFormVersion','sourceMessageId','sourceTalkNodeId')};standard['response']=response
            text=json.dumps(standard,ensure_ascii=False,sort_keys=True);addressed=True
        text=text.strip();tenant=proof['tenant']
        event={'eventId':'teams:'+state.scoped_hash(json.dumps([conversation_id,event_id])),'nativeRoomKey':'teams:'+state.scoped_hash(json.dumps([tenant,conversation_id])),
            'nativeParticipantKey':'teams:'+kind+':'+state.scoped_hash(json.dumps([tenant,sender_id])),'participantKind':kind,'participantName':str(activity['from'].get('name') or sender_id)[:120],
            'roomMode':'private' if mode=='personal' else 'shared','addressed':addressed,'message':text,'occurredAt':stamp.astimezone(timezone.utc).isoformat()}
        state._event(event);return event

    def _accept(self,binding,event,context):
        cid,generation,lease_id=binding
        with self.sessions() as db:
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.id==lease_id).with_for_update())
            if lease is None or not lease.owner or lease.lease_expires_at is None or lease.lease_expires_at<=state.clock(db):raise HTTPException(503,'TEAMS_RECEIVER_NOT_READY')
            row=state._connection(db,cid)
            if row.generation!=generation or row.desired_state!='connected':raise HTTPException(409,'CONNECTION_CHANGED')
            if row.observed_state!='connected':return None
            existing=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==cid,ChannelConnectionDelivery.event_key==state.scoped_hash(event['eventId'])))
            item=state.accept_delivery(db,cid,lease_id,lease.owner,lease.fence,event)
            if existing is None and item.status=='received' and event['addressed']:
                busy=db.scalar(select(ChannelConnectionDelivery.id).where(ChannelConnectionDelivery.connection_id==cid,
                    ChannelConnectionDelivery.id!=item.id,ChannelConnectionDelivery.generation==generation,
                    ChannelConnectionDelivery.native_room_hash==item.native_room_hash,
                    ChannelConnectionDelivery.status.in_(['received','executing','ready','sending','unknown']),
                    ChannelConnectionDelivery.event_json['addressed'].astext=='true').limit(1))
                if busy is not None:item.status='failed';item.last_error_code='ROOM_BUSY'
            if item.context_ciphertext is None:
                item.context_ciphertext=encrypt_connection_state(context,row.organization_id,row.id,settings.channel_connection_encryption_key,settings.channel_connection_key_version,'delivery:'+str(item.id));item.context_key_version=settings.channel_connection_key_version
            result={'delivery_id':str(item.id)};db.commit();return result

    async def receive(self,authorization,activity):
        try:
            parts=authorization.split(' ')
            if len(parts)!=2 or parts[0].lower()!='bearer' or len(authorization)>20000:raise ValueError()
            client=_uuid(jwt.decode(parts[1],options={'verify_signature':False})['aud'])
        except (jwt.PyJWTError,ValueError,TypeError,KeyError,HTTPException):raise HTTPException(401,'TEAMS_AUTHENTICATION_FAILED') from None
        if not await asyncio.to_thread(self._client_candidate,client):raise HTTPException(401,'TEAMS_AUTHENTICATION_FAILED')
        verified=await asyncio.to_thread(self.authenticator.authenticate,authorization,activity,client)
        if activity.get('type')=='invoke' and activity.get('name')=='adaptiveCard/action':
            value=activity.get('value')
            if not isinstance(value,dict):raise HTTPException(422,'TEAMS_CARD_RESPONSE_INVALID')
            if value.get('trigger')=='automatic':return {'ignored':True}
            if value.get('trigger') not in {None,'manual'}:raise HTTPException(422,'TEAMS_CARD_RESPONSE_INVALID')
            action=value.get('action')
            if not isinstance(action,dict) or action.get('type')!='Action.Execute' or not isinstance(action.get('data'),dict):raise HTTPException(422,'TEAMS_CARD_RESPONSE_INVALID')
            activity={**activity,'type':'message','value':action['data']}
        if activity.get('type')!='message':return {'ignored':True}
        source=activity.get('from');recipient=activity.get('recipient')
        if isinstance(source,dict) and isinstance(recipient,dict) and source.get('id')==recipient.get('id')=='28:'+client:return {'ignored':True}
        binding,proof,conversation,sender,recipient=await asyncio.to_thread(self._bind_activity,client,verified,activity)
        if sender==recipient:return {'ignored':True}
        event=self._event(activity,proof,conversation,sender,recipient)
        context={**proof,'conversation_id':conversation,'activity_id':activity['id'],'sender_id':sender,'recipient_id':recipient,'generation':binding[1]}
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=await asyncio.to_thread(self._accept,binding,event,context)
            if result is not None:return result
            await asyncio.sleep(.05)
        raise HTTPException(503,'TEAMS_RECEIVER_NOT_READY')


class TeamsListener:
    """HTTP route가 직접 원장에 commit한다. 이 객체는 lease 수명과 회신을 소유한다."""
    def __init__(self,adapter,targets):self.adapter=adapter;self.targets={t.spec.id:t for t in targets};self.stopped=asyncio.Event()
    async def update(self,targets):self.targets={t.spec.id:t for t in targets}
    async def listen(self,accept):await self.stopped.wait()
    async def close(self):self.stopped.set()
    async def send(self,target,delivery_id,packets,context):
        try:
            if self.stopped.is_set() or self.targets.get(target.spec.id)!=target:raise ValueError()
            if (context.get('tenant'),context.get('client'),context.get('target'),context.get('generation'))!=(target.spec.installation_id,target.spec.account_id,target.spec.target_id,target.spec.generation):raise ValueError()
            service=validate_teams_service_url(context.get('service_url'))
            if not isinstance(packets,list) or len(packets)>20:raise ValueError()
            prepared=[]
            for message in packets:
                packet={**message,'from':{'id':context['recipient_id']},'recipient':{'id':context['sender_id']},'conversation':{'id':context['conversation_id']},'replyToId':context['activity_id']}
                if len(json.dumps(packet,ensure_ascii=False).encode())>28000:raise ValueError()
                prepared.append(packet)
            token=await self.adapter._token(target.transport.credentials)
        except (HTTPException,ValueError,TypeError,KeyError):raise NativeNotSubmitted() from None
        receipts=[]
        for packet in prepared:
            result=await asyncio.to_thread(self.adapter.http.send,service,context['conversation_id'],context['activity_id'],token,packet)
            receipts.append(_text(result.get('id')))
        return receipts
