from importlib import import_module
from copy import deepcopy
from uuid import uuid4
import asyncio,json
import pytest
from fastapi import HTTPException
from app.services.channel_connection_supervisor import ConnectionSpec


def subject():return import_module('app.services.channel_connection_discord')


def spec(**changes):return ConnectionSpec(**{'id':uuid4(),'organization_id':uuid4(),'provider':'discord','generation':1,'installation_id':'300','account_id':'200','target_id':'400',**changes})


class Http:
    def __init__(self):
        self.calls=[];self.responses={'/users/@me':{'id':'200','bot':True},'/oauth2/applications/@me':{'id':'100','bot':{'id':'200'}},
         '/channels/400':{'id':'400','guild_id':'300','type':0,'permission_overwrites':[]},
         '/guilds/300':{'id':'300','owner_id':'900','roles':[{'id':'300','permissions':'3072'}]},
         '/guilds/300/members/200':{'user':{'id':'200'},'roles':[]}}
    def call(self,method,path,token,payload=None):self.calls.append((method,path,token,payload));return deepcopy(self.responses[path])


def test_verified_bot_app_guild_channel_and_permissions_use_existing_fields():
    async def run():
        h=Http();s=spec();target=await subject().DiscordAdapter(None,owner='c05-qa',http=h).verify(s,{'bot_token':'synthetic-only'})
        assert target.spec==s and target.listener_scope=='discord-app:100' and target.exclusive_scope
        assert target.transport.bot_user_id=='200' and target.transport.application_id=='100' and target.transport.guild_id=='300'
        assert all(v[0]=='GET' and v[2]=='synthetic-only' for v in h.calls) and 'synthetic-only' not in repr(target.transport)
    asyncio.run(run())


@pytest.mark.parametrize('path,field,value',[
 ('/users/@me','bot',False),('/users/@me','id','201'),('/oauth2/applications/@me','bot',{'id':'201'}),
 ('/channels/400','id','401'),('/channels/400','guild_id','301'),('/channels/400','type',2),
 ('/guilds/300','id','301'),('/guilds/300/members/200','user',{'id':'201'}),
 ('/guilds/300','roles',[{'id':'300','permissions':'1024'}]),('/guilds/300','roles',[{'id':'300','permissions':'2048'}])])
def test_unverified_identity_or_missing_access_is_rejected(path,field,value):
    async def run():
        h=Http();h.responses[path][field]=value
        with pytest.raises(HTTPException):await subject().DiscordAdapter(None,owner='c05',http=h).verify(spec(),{'bot_token':'synthetic-only'})
    asyncio.run(run())


def test_effective_permissions_follow_everyone_roles_then_member_override():
    m=subject();guild={'id':'300','owner_id':'900','roles':[{'id':'300','permissions':'3072'},{'id':'301','permissions':'0'}]};member={'user':{'id':'200'},'roles':['301']}
    channel={'permission_overwrites':[{'id':'300','type':0,'deny':'2048','allow':'0'},{'id':'301','type':0,'deny':'0','allow':'2048'},{'id':'200','type':1,'deny':'2048','allow':'0'}]}
    assert m.effective_permissions(guild,member,channel,'200')&2048==0
    channel['permission_overwrites'].pop();assert m.effective_permissions(guild,member,channel,'200')&2048
    guild['roles'][1]['permissions']='8';channel['permission_overwrites']=[{'id':'200','type':1,'deny':'3072','allow':'0'}]
    assert m.effective_permissions(guild,member,channel,'200')&3072==3072


def test_threads_use_parent_overwrites_and_send_in_threads_permission():
    async def run():
        h=Http();h.responses['/channels/400']={'id':'400','guild_id':'300','type':11,'parent_id':'401','thread_metadata':{'archived':False,'locked':False}}
        h.responses['/channels/401']={'id':'401','guild_id':'300','type':0,'permission_overwrites':[]};h.responses['/guilds/300']['roles'][0]['permissions']=str(1024+(1<<38))
        a=subject().DiscordAdapter(None,owner='c05',http=h);result=await a.verify(spec(),{'bot_token':'synthetic-only'});assert result.transport.channel_type==11
        h.responses['/channels/401']['permission_overwrites']=[{'id':'200','type':1,'deny':str(1<<38),'allow':'0'}]
        with pytest.raises(HTTPException):await a.verify(spec(),{'bot_token':'synthetic-only'})
    asyncio.run(run())


def test_existing_private_dm_has_no_guild_and_is_not_created_automatically():
    async def run():
        h=Http();h.responses['/channels/400']={'id':'400','type':1,'recipients':[{'id':'500'}]}
        a=subject().DiscordAdapter(None,owner='c05',http=h);target=await a.verify(spec(installation_id='DM'),{'bot_token':'synthetic-only'})
        assert target.transport.guild_id is None and target.transport.channel_type==1 and all(x[0]=='GET' for x in h.calls)
    asyncio.run(run())


def test_rest_transport_binds_bot_auth_to_fixed_discord_api_paths():
    seen=[]
    def request(method,url,headers,raw):seen.append((method,url,headers,raw));return 200,{'id':'200','bot':True}
    h=subject().DiscordHttpTransport(request=request);assert h.call('GET','/users/@me','synthetic-only')['id']=='200'
    assert seen[0][1]=='https://discord.com/api/v10/users/@me' and seen[0][2]['Authorization']=='Bot synthetic-only'
    for path in ['https://attacker.invalid','/channels/400/../../users/@me','/channels/400?token=synthetic','/webhooks/400/secret']:
        with pytest.raises(HTTPException):h.call('GET',path,'synthetic-only')
    assert len(seen)==1


def test_private_thread_requires_existing_bot_membership():
    async def run():
        h=Http();h.responses['/channels/400']={'id':'400','guild_id':'300','type':12,'parent_id':'401','thread_metadata':{'archived':False,'locked':False}}
        h.responses['/channels/401']={'id':'401','guild_id':'300','type':0,'permission_overwrites':[]};h.responses['/guilds/300']['roles'][0]['permissions']=str(1024+(1<<38))
        h.responses['/channels/400/thread-members/200']={'id':'400','user_id':'200'}
        a=subject().DiscordAdapter(None,owner='c05',http=h);assert (await a.verify(spec(),{'bot_token':'synthetic-only'})).transport.channel_type==12
        h.responses['/channels/400/thread-members/200']['user_id']='201'
        with pytest.raises(HTTPException):await a.verify(spec(),{'bot_token':'synthetic-only'})
    asyncio.run(run())
