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


class Ws:
    def __init__(self,app='AQA001'):
        self.queue=asyncio.Queue();self.queue.put_nowait(json.dumps({'type':'hello','connection_info':{'app_id':app}}));self.sent=[];self.closed=False
    async def recv(self):return await self.queue.get()
    async def send(self,value):self.sent.append(json.loads(value))
    async def close(self):self.closed=True


class Socket:
    def __init__(self,ws):self.ws=ws;self.urls=[]
    async def open(self,url):self.urls.append(url);return self.ws


def socket_frame(event='message'):
    import time
    stamp=f'{time.time():.6f}'
    return {'type':'events_api','envelope_id':'synthetic-envelope','payload':{'type':'event_callback','api_app_id':'AQA001','team_id':'TQA001','event':{'type':event,'channel':'CQA001','user':'UQA002','text':'<@UQA001> 검증 질문','ts':stamp}}}


async def listener(ws=None):
    h=Http();h.responses['apps.connections.open']={'ok':True,'url':'wss://wss.slack.com/link/?ticket=synthetic-only'}
    sock=Socket(ws or Ws());a=subject().SlackAdapter(None,owner='c04-qa',http=h,socket=sock)
    target=await a.verify(spec(),{'bot_token':'synthetic-bot-token','app_token':'synthetic-app-token'})
    return await a.open([target]),h,sock,target


def test_socket_hello_verifies_app_before_listener_is_ready():
    async def run():
        live,h,sock,target=await listener()
        assert h.calls[-1][:2]==('apps.connections.open','synthetic-app-token') and len(sock.urls)==1
        assert live.targets[target.spec.id]==target
        await live.close();assert sock.ws.closed
    asyncio.run(run())


def test_socket_wrong_app_is_closed_without_ready_state():
    async def run():
        ws=Ws('AOTHER')
        with pytest.raises(HTTPException):await listener(ws)
        assert ws.closed
    asyncio.run(run())


def test_socket_ack_is_sent_only_after_sdk_event_is_committed():
    async def run():
        live,h,sock,target=await listener();seen=[]
        async def accept(envelopes):
            assert not sock.ws.sent;seen.extend(envelopes);return [uuid4()]
        try:
            await live.handle(socket_frame(),accept)
            assert sock.ws.sent==[{'envelope_id':'synthetic-envelope'}]
            assert len(seen)==1 and seen[0].connection_id==target.spec.id
            event=seen[0].event;assert event['message']=='검증 질문' and event['addressed'] and event['occurredAt']
            assert event['channelId']=='CQA001' and 'synthetic-bot-token' not in str(seen[0])
        finally:await live.close()
    asyncio.run(run())


def test_socket_commit_failure_never_acknowledges_the_question():
    async def run():
        live,h,sock,target=await listener()
        async def fail(envelopes):raise RuntimeError('synthetic commit unknown')
        try:
            with pytest.raises(RuntimeError):await live.handle(socket_frame(),fail)
            assert not sock.ws.sent
        finally:await live.close()
    asyncio.run(run())


def test_socket_ignores_self_and_unconfigured_targets_without_cga_execution():
    async def run():
        live,h,sock,target=await listener();seen=[]
        async def accept(envelopes):seen.extend(envelopes);return [uuid4()]
        try:
            body=socket_frame();body['payload']['event']['user']='UQA001';await live.handle(body,accept)
            body=socket_frame();body['payload']['event']['channel']='COTHER';await live.handle(body,accept)
            assert not seen and len(sock.ws.sent)==2
            body=socket_frame();body['payload']['event']['text']='일반 대화';await live.handle(body,accept)
            assert len(seen)==1 and not seen[0].event['addressed']
        finally:await live.close()
    asyncio.run(run())


def test_default_socket_transport_disables_redirects_proxy_and_sensitive_logs(monkeypatch):
    async def run():
        import websockets.asyncio.client as client
        from websockets.exceptions import SecurityError
        calls=[];ws=Ws()
        class Connect:
            def __init__(self,url,**options):calls.append((url,options,self))
            def __await__(self):
                async def result():return ws
                return result().__await__()
        monkeypatch.setattr(client,'connect',Connect)
        result=await subject().SlackSocketTransport().open('wss://wss.slack.com/link/?ticket=synthetic-only')
        assert result is ws and len(calls)==1
        url,options,transport=calls[0]
        assert options['proxy'] is None and options['max_size']==262144 and options['max_queue']==8 and options['logger'].disabled and not options['logger'].propagate
        assert options['open_timeout']==5 and options['close_timeout']==2 and options['compression'] is None
        assert isinstance(transport.process_redirect(ValueError('synthetic')),SecurityError)
    asyncio.run(run())


def test_cold_start_skips_wrong_app_token_before_opening_valid_shared_socket():
    async def run():
        from dataclasses import replace
        h=Http();valid=Ws();bad_ws=Ws('AOTHER')
        class Multiple:
            async def open(self,url):return bad_ws if 'other-app' in url else valid
        original=h.call
        def call(method,token,payload):
            if method=='apps.connections.open':return {'ok':True,'url':'wss://wss.slack.com/link/?ticket='+('other-app' if token=='synthetic-other-app-token' else 'valid')}
            return original(method,token,payload)
        h.call=call;a=subject().SlackAdapter(None,owner='c04',http=h,socket=Multiple())
        good=await a.verify(spec(),{'bot_token':'synthetic-bot-token','app_token':'synthetic-app-token'})
        bad=replace(good,spec=spec(target_id='CQA002'),transport=replace(good.transport,credentials={'bot_token':'synthetic-bot-token','app_token':'synthetic-other-app-token'}))
        live=await a.open([bad,good])
        try:assert bad_ws.closed and not valid.closed and list(live.targets)==[good.spec.id] and not live.accepts_target(bad)
        finally:await live.close()
    asyncio.run(run())


def test_child_runtime_failure_is_not_acknowledged_as_invalid_input(monkeypatch):
    async def run():
        live,h,sock,target=await listener()
        def unavailable(*args):raise HTTPException(422,'CHANNEL_PACKET_INVALID')
        monkeypatch.setattr(subject(),'convert_channel_packet',unavailable)
        async def accept(items):raise AssertionError('must not execute')
        try:
            with pytest.raises(HTTPException):await live.handle(socket_frame(),accept)
            assert not sock.ws.sent
        finally:await live.close()
    asyncio.run(run())
