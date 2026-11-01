"""Slack Socket Mode의 고정 API·검증 경계. 실제 실행은 승인된 서버 설정에서만 켠다."""
from __future__ import annotations
import asyncio,json,re,logging,hashlib,time
from datetime import datetime,timezone
from dataclasses import dataclass,field
from urllib.parse import urlsplit,parse_qs,urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler
from fastapi import HTTPException
from sqlalchemy import select
from app.models import ChannelInstallationLease
from app.services import channel_connection_state as state
from app.services.channel_connection_supervisor import VerifiedTarget,InboundEnvelope,NativeNotSubmitted
from app.services.channel_connection_bridge import convert_channel_packet,NativePacketRejected

METHODS=frozenset({'auth.test','bots.info','conversations.info','apps.connections.open','chat.postMessage'})
SOCKET_HOSTS=frozenset({'wss.slack.com','wss-primary.slack.com'})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class SlackApiRejected(Exception):
    """Slack가 해당 요청의 거부를 명시했다. 예외 원문은 저장하지 않는다."""


def _post(url,headers,body):
    try:
        with build_opener(_NoRedirect()).open(Request(url,data=body,headers=headers,method='POST' if body is not None else 'GET'),timeout=5) as response:
            raw=response.read(1048577)
            if not 200<=response.status<300 or len(raw)>1048576:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except Exception:raise HTTPException(503,'SLACK_TRANSPORT_FAILED') from None


class SlackHttpTransport:
    def __init__(self,*,post=_post):self.post=post
    def call(self,method,token,payload):
        if method not in METHODS or not isinstance(token,str) or not token or len(token)>24000 or any(c.isspace() for c in token) or not isinstance(payload,dict):raise HTTPException(422,'SLACK_CONFIGURATION_INVALID')
        raw=json.dumps(payload,ensure_ascii=False).encode()
        if len(raw)>1000000:raise HTTPException(422,'SLACK_PACKET_LIMIT')
        url='https://slack.com/api/'+method
        if method in {'bots.info','conversations.info'}:url+='?'+urlencode(payload);raw=None
        value=self.post(url,{'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'},raw)
        if not isinstance(value,dict):raise HTTPException(503,'SLACK_TRANSPORT_FAILED')
        if value.get('ok') is not True:
            definite={'invalid_auth','token_revoked','token_expired','account_inactive','channel_not_found','not_in_channel','is_archived','missing_scope','not_authed','no_permission','not_allowed_token_type','invalid_arguments','msg_too_long','invalid_blocks'}
            if value.get('error') in definite:raise SlackApiRejected() from None
            raise HTTPException(503,'SLACK_TRANSPORT_FAILED')
        return value


def validate_socket_url(value):
    try:
        if not isinstance(value,str) or len(value)>4096 or any(c.isspace() for c in value) or '\\' in value:raise ValueError()
        p=urlsplit(value);q=parse_qs(p.query,strict_parsing=True)
        if p.scheme!='wss' or p.hostname not in SOCKET_HOSTS or p.port not in {None,443} or p.username is not None or p.password is not None or p.path!='/link/' or p.fragment or not q.get('ticket') or len(q['ticket'])!=1:raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(403,'SLACK_SOCKET_DESTINATION_UNTRUSTED') from None


def _id(value,prefix):
    if not isinstance(value,str) or not re.fullmatch('['+prefix+'][A-Z0-9]{2,63}',value):raise HTTPException(422,'SLACK_IDENTITY_INVALID')
    return value


@dataclass(frozen=True)
class SlackBinding:
    credentials:dict=field(repr=False)
    team_id:str
    bot_user_id:str
    bot_id:str
    app_id:str


class SlackAdapter:
    def __init__(self,sessions,*,owner,http=None,socket=None):self.sessions=sessions;self.owner=owner;self.http=http or SlackHttpTransport();self.socket=socket or SlackSocketTransport();self.open_not_before={}
    async def verify(self,spec,credentials):
        try:
            bot_token=credentials.get('bot_token');app_token=credentials.get('app_token')
            if spec.provider!='slack' or not all(isinstance(t,str) and t and len(t)<=24000 and not any(c.isspace() for c in t) for t in [bot_token,app_token]):raise ValueError()
            identity=await asyncio.to_thread(self.http.call,'auth.test',bot_token,{})
            team=_id(identity.get('team_id'),'T');user=_id(identity.get('user_id'),'UW');bot=_id(identity.get('bot_id'),'B')
            if (team,user)!=(spec.installation_id,spec.account_id):raise ValueError()
            info=await asyncio.to_thread(self.http.call,'bots.info',bot_token,{'bot':bot});info=info.get('bot')
            if not isinstance(info,dict) or info.get('id')!=bot or info.get('user_id')!=user or info.get('deleted',False) is not False:raise ValueError()
            app=_id(info.get('app_id'),'A')
            channel=await asyncio.to_thread(self.http.call,'conversations.info',bot_token,{'channel':_id(spec.target_id,'CGD')});channel=channel.get('channel')
            if not isinstance(channel,dict) or channel.get('id')!=spec.target_id or channel.get('is_archived',False) is not False or not (channel.get('is_member') is True or channel.get('is_im') is True and spec.target_id.startswith('D')):raise ValueError()
            return VerifiedTarget(spec,team,user,'slack-app:'+app,SlackBinding(dict(credentials),team,user,bot,app),True)
        except (ValueError,TypeError,KeyError,SlackApiRejected):raise HTTPException(403,'SLACK_IDENTITY_VERIFICATION_FAILED') from None


    async def open(self,targets):
        if not targets or any(t.transport.app_id!=targets[0].transport.app_id for t in targets):raise HTTPException(403,'SLACK_SOCKET_SCOPE_MISMATCH')
        app=targets[0].transport.app_id
        if self.open_not_before.get(app,0)>time.monotonic():raise HTTPException(503,'SLACK_SOCKET_RECONNECT_DELAY')
        attempted=set()
        for target in targets:
            token=target.transport.credentials['app_token'];digest=hashlib.sha256(token.encode()).hexdigest()
            if digest in attempted:continue
            attempted.add(digest);ws=None
            try:
                data=await asyncio.to_thread(self.http.call,'apps.connections.open',token,{})
                ws=await self.socket.open(validate_socket_url(data.get('url')))
                hello=_frame(await asyncio.wait_for(ws.recv(),5))
                if hello.get('type')!='hello' or not isinstance(hello.get('connection_info'),dict) or hello['connection_info'].get('app_id')!=app:raise ValueError()
                self.open_not_before.pop(app,None)
                return SlackListener(self,targets,ws,digest)
            except BaseException as error:
                if ws is not None:
                    try:await ws.close()
                    except Exception:pass
                if isinstance(error,asyncio.CancelledError):raise
        self.open_not_before[app]=time.monotonic()+5
        raise HTTPException(503,'SLACK_SOCKET_VERIFICATION_FAILED') from None


def _frame(raw):
    try:
        if not isinstance(raw,(str,bytes)) or len(raw.encode() if isinstance(raw,str) else raw)>262144:raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError):raise HTTPException(422,'SLACK_ENVELOPE_INVALID') from None


class SlackSocketTransport:
    async def open(self,url):
        from websockets.asyncio.client import connect
        from websockets.exceptions import SecurityError
        class StrictConnect(connect):
            def process_redirect(self,error):return SecurityError('SLACK_SOCKET_REDIRECT_FORBIDDEN')
        logger=logging.getLogger('cga.private.slack.websocket');logger.disabled=True;logger.propagate=False
        try:
            return await StrictConnect(validate_socket_url(url),proxy=None,open_timeout=5,ping_interval=20,ping_timeout=10,close_timeout=2,max_size=262144,max_queue=8,compression=None,logger=logger)
        except Exception:raise HTTPException(503,'SLACK_SOCKET_UNAVAILABLE') from None


class SlackListener:
    def __init__(self,adapter,targets,ws,app_token_digest):
        self.adapter=adapter;self.ws=ws;self.app_id=targets[0].transport.app_id;self.app_token_digest=app_token_digest;self.stopped=False
        self.targets={t.spec.id:t for t in targets if self.accepts_target(t)}
    def accepts_target(self,target):
        return target.transport.app_id==self.app_id and hashlib.sha256(target.transport.credentials['app_token'].encode()).hexdigest()==self.app_token_digest
    async def update(self,targets):self.targets={t.spec.id:t for t in targets if self.accepts_target(t)}
    async def close(self):self.stopped=True;await self.ws.close()
    async def listen(self,accept):
        while not self.stopped:
            value=_frame(await self.ws.recv())
            if value.get('type')=='disconnect':raise RuntimeError('SLACK_SOCKET_REFRESH_REQUIRED')
            await asyncio.wait_for(self.handle(value,accept),2.5)
    async def handle(self,value,accept):
        if self.stopped:raise HTTPException(409,'SLACK_LISTENER_STOPPED')
        if not isinstance(value,dict) or not isinstance(value.get('envelope_id'),str) or not 0<len(value['envelope_id'])<=512:raise HTTPException(422,'SLACK_ENVELOPE_INVALID')
        try:await self._handle_payload(value,accept)
        except NativePacketRejected:pass
        except HTTPException as error:
            if error.status_code not in {404,409} or error.detail not in {'CONNECTION_CHANGED','CONNECTION_TARGET_INACTIVE','CONNECTION_NOT_FOUND'}:raise
        await self.ws.send(json.dumps({'envelope_id':value['envelope_id']}))

    async def _handle_payload(self,value,accept):
        payload=value.get('payload')
        if value.get('type') in {'events_api','interactive'}:
            if not isinstance(payload,dict) or payload.get('api_app_id')!=self.app_id:raise HTTPException(403,'SLACK_APP_SCOPE_MISMATCH')
            team=payload.get('team_id') or (payload.get('team') or {}).get('id')
            event=payload.get('event') if payload.get('type')=='event_callback' else payload
            if not isinstance(event,dict):raise HTTPException(422,'SLACK_ENVELOPE_INVALID')
            channel=event.get('channel')
            if isinstance(channel,dict):channel=channel.get('id')
            targets=[t for t in self.targets.values() if (t.spec.installation_id,t.spec.target_id)==(team,channel)]
            if len(targets)>1:raise HTTPException(409,'SLACK_TARGET_AMBIGUOUS')
            if targets:
                target=targets[0];binding=target.transport
                normalized=await asyncio.to_thread(convert_channel_packet,'slack','normalize',{'input':payload,'connection':{'teamId':binding.team_id,'botUserId':binding.bot_user_id,'botId':binding.bot_id}})
                if normalized is not None:
                    # 기존 원본/노드 검증 JSON은 유지한다. choiceIndex 방식과 혼합하지 않는다.
                    normalized.pop('sourceMessageId',None)
                    stamp=event.get('event_ts') or event.get('ts')
                    if payload.get('type')=='block_actions':stamp=(payload.get('actions') or [{}])[0].get('action_ts')
                    try:
                        if not isinstance(stamp,str) or not re.fullmatch(r'[0-9]{1,12}\.[0-9]{1,9}',stamp):raise ValueError()
                        normalized['occurredAt']=datetime.fromtimestamp(float(stamp),timezone.utc).isoformat()
                    except (ValueError,OverflowError,TypeError):raise HTTPException(422,'SLACK_EVENT_TIME_INVALID') from None
                    context={'team_id':team,'app_id':self.app_id,'bot_user_id':binding.bot_user_id,'channel_id':channel,'thread_ts':normalized.get('threadTs'),'generation':target.spec.generation}
                    committed=await accept([InboundEnvelope(target.spec.id,normalized,context)])
                    if not isinstance(committed,list) or len(committed)!=1:raise RuntimeError('SLACK_COMMIT_UNCONFIRMED')
        # 지원하지 않는 앱 이벤트는 실행하지 않는다. 질문은 handle에서 commit 확인 후 ACK한다.


    def _assert_live(self,target,delivery_id):
        with self.adapter.sessions() as db:
            lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==target.spec.organization_id,
                ChannelInstallationLease.provider=='slack',ChannelInstallationLease.listener_scope_hash==state.scoped_hash('verified-listener:slack:'+target.listener_scope)).with_for_update())
            if lease is None:raise HTTPException(409,'CONNECTION_LEASE_LOST')
            row=state._live(db,target.spec.id,lease.id,self.adapter.owner,lease.fence);item=state._delivery(db,delivery_id)
            if row.generation!=target.spec.generation or item.connection_id!=row.id or item.generation!=row.generation or item.status!='sending' or item.installation_fence!=lease.fence:raise HTTPException(409,'CONNECTION_CHANGED')
            db.commit()

    async def send(self,target,delivery_id,packets,context):
        try:
            if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
            binding=target.transport
            if (context.get('team_id'),context.get('app_id'),context.get('bot_user_id'),context.get('channel_id'),context.get('generation'))!=(binding.team_id,binding.app_id,binding.bot_user_id,target.spec.target_id,target.spec.generation):raise ValueError()
            if not isinstance(packets,list) or len(packets)>20:raise ValueError()
            for packet in packets:
                if not isinstance(packet,dict) or set(packet)-{'channel','text','thread_ts','blocks','mrkdwn','parse','unfurl_links','unfurl_media'} or packet.get('channel')!=context['channel_id'] or packet.get('thread_ts')!=context.get('thread_ts') or len(json.dumps(packet,ensure_ascii=False).encode())>1000000:raise ValueError()
        except (ValueError,TypeError,KeyError):raise NativeNotSubmitted() from None
        receipts=[]
        for packet in packets:
            try:
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
                await asyncio.to_thread(self._assert_live,target,delivery_id)
                if self.stopped or self.targets.get(target.spec.id)!=target:raise ValueError()
            except (HTTPException,ValueError):
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('SLACK_SEND_REVOKED') from None
            try:result=await asyncio.to_thread(self.adapter.http.call,'chat.postMessage',binding.credentials['bot_token'],packet)
            except SlackApiRejected:
                if not receipts:raise NativeNotSubmitted() from None
                raise RuntimeError('SLACK_PARTIAL_DELIVERY') from None
            stamp=result.get('ts')
            if result.get('channel')!=context['channel_id'] or not isinstance(stamp,str) or not re.fullmatch(r'[0-9]{1,12}\.[0-9]{1,9}',stamp):raise RuntimeError('SLACK_DELIVERY_RECEIPT_UNCONFIRMED')
            receipts.append(context['channel_id']+':'+stamp)
        return receipts
