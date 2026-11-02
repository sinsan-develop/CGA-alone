import asyncio,importlib
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException

BOT='U'+'1'*32;TARGET='C'+'2'*32

def subject():return importlib.import_module('app.services.channel_connection_line')

def test_verified_bot_info_and_existing_ids_are_required(monkeypatch):
    prepared=[]
    class Http:
        def bot_info(self,token):assert token=='synthetic-only';return {'userId':BOT}
    def prepare(self,spec,kind):prepared.append(kind);return True
    m=subject();monkeypatch.setattr(m.LineAdapter,'_prepare',prepare)
    spec=SimpleNamespace(id=uuid4(),organization_id=uuid4(),provider='line',generation=1,installation_id=BOT,account_id=BOT,target_id=TARGET)
    async def run():
        target=await m.LineAdapter(None,owner='qa',http=Http()).verify(spec,{'access_token':'synthetic-only','channel_secret':'synthetic-secret'})
        assert target.listener_scope=='line-bot:'+BOT and target.exclusive_scope and target.transport.source_type=='group'
        with pytest.raises(HTTPException):await m.LineAdapter(None,owner='qa',http=Http()).verify(SimpleNamespace(**{**vars(spec),'account_id':'U'+'3'*32}),{'access_token':'synthetic-only','channel_secret':'synthetic-secret'})
    asyncio.run(run());assert prepared==['group']

@pytest.mark.parametrize('target,kind',[(BOT,'user'),(TARGET,'group'),('R'+'a'*32,'room')])
def test_canonical_source_kind(target,kind):assert subject().source_type(target)==kind

@pytest.mark.parametrize('target',['https://untrusted.invalid','C../private','123','U'+'Z'*32,'C'+'1'*31])
def test_invalid_native_source_never_routes(target):
    with pytest.raises(HTTPException):subject().source_type(target)

def test_fixed_http_destinations_and_single_reply_request():
    calls=[]
    def request(*args):calls.append(args);return {'userId':BOT}
    h=subject().LineHttpTransport(request=request);h.bot_info('synthetic-only');h.reply('synthetic-only',{'replyToken':'synthetic-reply','messages':[{'type':'text','text':'QA'}]})
    assert [(x[0],x[1]) for x in calls]==[('GET','https://api.line.me/v2/bot/info'),('POST','https://api.line.me/v2/bot/message/reply')]
    with pytest.raises(HTTPException):h.reply('synthetic-only',{'replyToken':'x','messages':[{'type':'text','text':'QA'}]*6})
    assert len(calls)==2


@pytest.mark.parametrize('age,redelivery,event_age,expected',[(54.999,False,0,True),(55,False,0,False),(-1,False,0,False),(1,True,1199.999,True),(1,True,1200,False),(1,True,-1,False)])
def test_reply_time_boundaries_use_original_server_receipt(age,redelivery,event_age,expected):
    assert subject().LineListener._window({'received_at':2000000-age},{'redelivery':redelivery,'timestamp':int((2000000-event_age)*1000)},2000000) is expected
