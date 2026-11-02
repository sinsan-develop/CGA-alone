"""Discord Bot API의 고정 통신·신원·권한 경계. 사용자 토큰과 자동 권한 변경은 없다."""
from __future__ import annotations
import asyncio,json,re
from dataclasses import dataclass,field
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from fastapi import HTTPException
from app.services.channel_connection_supervisor import VerifiedTarget

API='https://discord.com/api/v10'
VIEW=1<<10
SEND=1<<11
EMBED=1<<14
SEND_THREAD=1<<38


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class DiscordApiRejected(Exception):
    """Discord가 미처리를 확정한 요청이다. 응답/URL/토큰 원문을 저장하지 않는다."""


def _request(method,url,headers,raw):
    try:
        with build_opener(_NoRedirect()).open(Request(url,data=raw,headers=headers,method=method),timeout=5) as response:
            body=response.read(1048577)
            if len(body)>1048576:raise ValueError()
            return response.status,json.loads(body) if body else None
    except HTTPError as error:
        try:code=json.loads(error.read(1048577)).get('code')
        except Exception:code=None
        if error.code in {401,403,404,429} or error.code==400 and code in {10003,10008,10062,50001,50013,50035,20016,20028}:raise DiscordApiRejected() from None
        raise HTTPException(503,'DISCORD_TRANSPORT_FAILED') from None
    except Exception:raise HTTPException(503,'DISCORD_TRANSPORT_FAILED') from None


def _id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value) or int(value)>18446744073709551615:raise HTTPException(422,'DISCORD_IDENTITY_INVALID')
    return value


def _token(value):
    if not isinstance(value,str) or not value or len(value)>24000 or any(c.isspace() for c in value):raise HTTPException(422,'DISCORD_CREDENTIALS_INVALID')
    return value


class DiscordHttpTransport:
    def __init__(self,*,request=_request):self.request=request
    def call(self,method,path,token,payload=None):
        static={'/users/@me','/oauth2/applications/@me','/gateway/bot'}
        ids=r'[1-9][0-9]{0,19}'
        valid=method=='GET' and (path in static or re.fullmatch('/channels/'+ids,path) or re.fullmatch('/guilds/'+ids,path) or re.fullmatch('/guilds/'+ids+'/members/'+ids,path) or re.fullmatch('/channels/'+ids+'/thread-members/'+ids,path))
        valid=valid or method=='POST' and re.fullmatch('/channels/'+ids+'/messages',path)
        if not valid:raise HTTPException(422,'DISCORD_ENDPOINT_INVALID')
        token=_token(token)
        if payload is not None and not isinstance(payload,dict):raise HTTPException(422,'DISCORD_PACKET_INVALID')
        raw=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
        if raw is not None and len(raw)>1000000:raise HTTPException(422,'DISCORD_PACKET_LIMIT')
        status,value=self.request(method,API+path,{'Authorization':'Bot '+token,'Content-Type':'application/json','Accept':'application/json','User-Agent':'DiscordBot (https://github.com/sinsan-develop/CGA-alone, 0.1)'},raw)
        if not 200<=status<300 or not isinstance(value,dict):raise HTTPException(503,'DISCORD_TRANSPORT_FAILED')
        return value


    def ack(self,interaction_id,token):
        from urllib.parse import quote
        iid=_id(interaction_id);token=_token(token)
        status,_=self.request('POST',API+'/interactions/'+iid+'/'+quote(token,safe='')+'/callback',{'Content-Type':'application/json'},b'{"type":6}')
        if status!=204:raise HTTPException(503,'DISCORD_CALLBACK_UNCONFIRMED')


def _bits(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9]{1,40}',value):raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    return int(value)


def effective_permissions(guild,member,channel,bot_id):
    if not all(isinstance(v,dict) for v in [guild,member,channel]) or (member.get('user') or {}).get('id')!=bot_id:raise HTTPException(403,'DISCORD_MEMBER_INVALID')
    gid=_id(guild.get('id'));roles=guild.get('roles');member_roles=member.get('roles')
    if not isinstance(roles,list) or not isinstance(member_roles,list):raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    role_ids={gid,*[_id(v) for v in member_roles]};base=0;seen=set()
    for role in roles:
        rid=_id(role.get('id'))
        if rid in seen:raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
        seen.add(rid)
        if rid in role_ids:base|=_bits(role.get('permissions'))
    if gid not in seen:raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    if guild.get('owner_id')==bot_id or base&8:return (1<<63)-1
    overwrites=channel.get('permission_overwrites',[])
    if not isinstance(overwrites,list):raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    everyone=[];role_denies=role_allows=0;individual=[]
    for overwrite in overwrites:
        oid=_id(overwrite.get('id'));kind=overwrite.get('type')
        deny=_bits(overwrite.get('deny'));allow=_bits(overwrite.get('allow'))
        if kind==0 and oid==gid:everyone.append((deny,allow))
        elif kind==0 and oid in role_ids:role_denies|=deny;role_allows|=allow
        elif kind==1 and oid==bot_id:individual.append((deny,allow))
        elif kind not in {0,1}:raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    if len(everyone)>1 or len(individual)>1:raise HTTPException(422,'DISCORD_PERMISSION_INVALID')
    for deny,allow in everyone:base=(base&~deny)|allow
    base=(base&~role_denies)|role_allows
    for deny,allow in individual:base=(base&~deny)|allow
    return base


@dataclass(frozen=True)
class DiscordBinding:
    credentials:dict=field(repr=False)
    application_id:str
    bot_user_id:str
    guild_id:str|None
    channel_id:str
    channel_type:int
    permissions:int


class DiscordAdapter:
    def __init__(self,sessions,*,owner,http=None,socket=None):
        from app.services.channel_connection_discord_gateway import DiscordSocketTransport
        self.sessions=sessions;self.owner=owner;self.http=http or DiscordHttpTransport();self.socket=socket or DiscordSocketTransport();self.open_not_before={}
    async def verify(self,spec,credentials):
        try:
            token=_token(credentials.get('bot_token'))
            if spec.provider!='discord':raise ValueError()
            user=await asyncio.to_thread(self.http.call,'GET','/users/@me',token)
            bot=_id(user.get('id'))
            if user.get('bot') is not True or bot!=spec.account_id:raise ValueError()
            app=await asyncio.to_thread(self.http.call,'GET','/oauth2/applications/@me',token);appid=_id(app.get('id'))
            if 'bot' in app and (not isinstance(app['bot'],dict) or app['bot'].get('id')!=bot):raise ValueError()
            cid=_id(spec.target_id);channel=await asyncio.to_thread(self.http.call,'GET','/channels/'+cid,token)
            kind=channel.get('type')
            if channel.get('id')!=cid or type(kind) is not int or kind not in {0,1,5,10,11,12}:raise ValueError()
            if kind==1:
                if spec.installation_id!='DM' or channel.get('guild_id') is not None or not isinstance(channel.get('recipients'),list) or len(channel['recipients'])!=1 or _id(channel['recipients'][0].get('id'))==bot:raise ValueError()
                guild_id=None;permissions=VIEW|SEND|EMBED
            else:
                guild_id=_id(channel.get('guild_id'))
                if guild_id!=spec.installation_id:raise ValueError()
                guild=await asyncio.to_thread(self.http.call,'GET','/guilds/'+guild_id,token)
                if guild.get('id')!=guild_id:raise ValueError()
                member=await asyncio.to_thread(self.http.call,'GET','/guilds/'+guild_id+'/members/'+bot,token)
                parent=channel
                if kind in {10,11,12}:
                    parent_id=_id(channel.get('parent_id'));parent=await asyncio.to_thread(self.http.call,'GET','/channels/'+parent_id,token)
                    meta=channel.get('thread_metadata')
                    if parent.get('id')!=parent_id or parent.get('guild_id')!=guild_id or parent.get('type') not in {0,5,15,16} or not isinstance(meta,dict) or meta.get('archived') is not False or meta.get('locked') is not False:raise ValueError()
                    if kind==12:
                        membership=await asyncio.to_thread(self.http.call,'GET','/channels/'+cid+'/thread-members/'+bot,token)
                        if membership.get('id')!=cid or membership.get('user_id')!=bot:raise ValueError()
                permissions=effective_permissions(guild,member,parent,bot)
                needed=VIEW|(SEND_THREAD if kind in {10,11,12} else SEND)
                if permissions&needed!=needed:raise ValueError()
            return VerifiedTarget(spec,spec.installation_id,bot,'discord-app:'+appid,DiscordBinding(dict(credentials),appid,bot,guild_id,cid,kind,permissions),True)
        except (ValueError,TypeError,KeyError,AttributeError,DiscordApiRejected):raise HTTPException(403,'DISCORD_IDENTITY_VERIFICATION_FAILED') from None


    async def open(self,targets):
        from sqlalchemy import select
        from app.models import ChannelInstallationLease
        from app.services import channel_connection_state as state
        from app.services.channel_connection_discord_gateway import DiscordState,open_gateway
        if not targets:raise HTTPException(422,'DISCORD_TARGET_MISSING')
        first=targets[0]
        def current():
            with self.sessions() as db:
                lease=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.organization_id==first.spec.organization_id,ChannelInstallationLease.provider=='discord',ChannelInstallationLease.listener_scope_hash==state.scoped_hash('verified-listener:discord:'+first.listener_scope)))
                if lease is None:raise HTTPException(409,'INSTALLATION_LEASE_LOST')
                state._installation(db,lease.id,self.owner,lease.fence);return lease.id,lease.fence
        lid,fence=await asyncio.to_thread(current)
        gate=DiscordState(self.sessions,lease_id=lid,owner=self.owner,fence=fence)
        listener=await open_gateway(self,targets,gate)
        try:
            if not listener.resumed:await asyncio.to_thread(gate.fresh_cutoff,targets)
            return listener
        except BaseException:
            await listener.close();raise
