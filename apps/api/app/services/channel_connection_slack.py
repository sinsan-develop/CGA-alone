"""Slack Socket Mode의 고정 API·검증 경계. 실제 실행은 승인된 서버 설정에서만 켠다."""
from __future__ import annotations
import asyncio,json,re
from dataclasses import dataclass,field
from urllib.parse import urlsplit,parse_qs,urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler
from fastapi import HTTPException
from app.services.channel_connection_supervisor import VerifiedTarget

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
    def __init__(self,sessions,*,owner,http=None):self.sessions=sessions;self.owner=owner;self.http=http or SlackHttpTransport()
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
