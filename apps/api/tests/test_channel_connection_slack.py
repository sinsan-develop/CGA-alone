from importlib import import_module
import asyncio,json
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app.services.channel_connection_supervisor import ConnectionSpec


def subject():return import_module('app.services.channel_connection_slack')


def spec(**changes):return ConnectionSpec(**{'id':uuid4(),'organization_id':uuid4(),'provider':'slack','generation':1,'installation_id':'TQA001','account_id':'UQA001','target_id':'CQA001',**changes})


class Http:
    def __init__(self):self.calls=[];self.responses={
        'auth.test':{'ok':True,'team_id':'TQA001','user_id':'UQA001','bot_id':'BQA001'},
        'bots.info':{'ok':True,'bot':{'id':'BQA001','app_id':'AQA001','user_id':'UQA001','deleted':False}},
        'conversations.info':{'ok':True,'channel':{'id':'CQA001','is_member':True,'is_archived':False}}}
    def call(self,method,token,payload):self.calls.append((method,token,payload));return self.responses[method]


def test_authentication_binds_workspace_bot_app_and_channel_without_opening_socket():
    async def run():
        h=Http();a=subject().SlackAdapter(None,owner='c04-qa',http=h);s=spec()
        target=await a.verify(s,{'bot_token':'synthetic-bot-token','app_token':'synthetic-app-token'})
        assert target.spec==s and target.installation_id=='TQA001' and target.account_id=='UQA001'
        assert target.listener_scope=='slack-app:AQA001' and target.exclusive_scope
        assert [x[0] for x in h.calls]==['auth.test','bots.info','conversations.info']
        assert all(x[1]=='synthetic-bot-token' for x in h.calls)
        assert 'synthetic-' not in repr(target.transport)
    asyncio.run(run())


@pytest.mark.parametrize('method,field,value',[
 ('auth.test','team_id','TOTHER'),('auth.test','user_id','UOTHER'),('auth.test','bot_id',None),
 ('bots.info','bot',{'id':'BOTHER','app_id':'AQA001','user_id':'UQA001'}),
 ('bots.info','bot',{'id':'BQA001','app_id':'AQA001','user_id':'UOTHER'}),
 ('bots.info','bot',{'id':'BQA001','app_id':'AQA001','user_id':'UQA001','deleted':True}),
 ('conversations.info','channel',{'id':'COTHER','is_member':True}),
 ('conversations.info','channel',{'id':'CQA001','is_member':False}),
 ('conversations.info','channel',{'id':'CQA001','is_member':True,'is_archived':True})])
def test_mismatched_or_unavailable_identity_never_verifies(method,field,value):
    async def run():
        h=Http();h.responses[method][field]=value
        with pytest.raises(HTTPException):await subject().SlackAdapter(None,owner='qa',http=h).verify(spec(),{'bot_token':'synthetic-bot-token','app_token':'synthetic-app-token'})
    asyncio.run(run())


def test_http_transport_only_sends_credentials_to_fixed_methods():
    calls=[]
    def post(url,headers,body):calls.append((url,headers,json.loads(body)));return {'ok':True}
    h=subject().SlackHttpTransport(post=post);h.call('auth.test','synthetic-only',{})
    assert calls==[('https://slack.com/api/auth.test',{'Authorization':'Bearer synthetic-only','Content-Type':'application/json','Accept':'application/json'}, {})]
    for method,token in [('https://attacker.invalid','synthetic-only'),('auth.test','bad token'),('auth.test','')]:
        with pytest.raises(HTTPException):h.call(method,token,{})
    assert len(calls)==1


@pytest.mark.parametrize('url',['ws://wss.slack.com/link/?ticket=synthetic','wss://attacker.invalid/link/?ticket=synthetic','wss://wss.slack.com:8443/link/?ticket=synthetic','wss://wss.slack.com/other?ticket=synthetic','wss://wss.slack.com/link/','wss://wss.slack.com/link/?ticket=synthetic#fragment'])
def test_socket_ticket_is_never_sent_to_an_untrusted_destination(url):
    with pytest.raises(HTTPException):subject().validate_socket_url(url)


def test_fixed_slack_socket_hosts_are_accepted():
    for host in ['wss.slack.com','wss-primary.slack.com']:
        value='wss://'+host+'/link/?ticket=synthetic-only'
        assert subject().validate_socket_url(value)==value


@pytest.mark.parametrize('code',['internal_error','fatal_error'])
def test_slack_ambiguous_error_is_not_reported_as_definite_rejection(code):
    h=subject().SlackHttpTransport(post=lambda *args:{'ok':False,'error':code})
    with pytest.raises(HTTPException) as error:h.call('chat.postMessage','synthetic-only',{'channel':'CQA001','text':'QA'})
    assert error.value.detail=='SLACK_TRANSPORT_FAILED'


def test_read_only_info_methods_use_documented_get_without_credentials_in_query():
    calls=[]
    def request(url,headers,body):calls.append((url,headers,body));return {'ok':True}
    subject().SlackHttpTransport(post=request).call('bots.info','synthetic-only',{'bot':'BQA001'})
    assert calls[0][0]=='https://slack.com/api/bots.info?bot=BQA001' and calls[0][2] is None
    assert calls[0][1]['Authorization']=='Bearer synthetic-only'
