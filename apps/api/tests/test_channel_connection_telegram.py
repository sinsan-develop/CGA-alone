from importlib import import_module
from copy import deepcopy
import asyncio,json
import pytest
from fastapi import HTTPException


def subject():return import_module('app.services.channel_connection_telegram')


class Http:
    def __init__(self):
        self.calls=[];self.responses={'getMe':{'id':100,'is_bot':True,'username':'CgaQaBot'},'getWebhookInfo':{'url':'','pending_update_count':0},'getChat':{'id':-300,'type':'supergroup','permissions':{'can_send_messages':True,'can_send_photos':True}},'getChatMember':{'status':'member','user':{'id':100,'is_bot':True}}}
    def call(self,method,token,payload):self.calls.append((method,token,payload));return deepcopy(self.responses[method])


def test_read_only_bot_identity_and_chat_access_are_verified_without_token_prefix_trust():
    async def run():
        m=subject();h=Http();identity=await m.verify_bot_identity(h,'synthetic-only');chat=await m.verify_chat_access(h,'synthetic-only',identity.bot_user_id,'-300');assert identity.bot_user_id=='100' and identity.bot_username=='CgaQaBot' and chat.chat_id=='-300' and chat.chat_type=='supergroup' and chat.can_send_photos
        assert [x[0] for x in h.calls]==['getMe','getChat','getChatMember']
        assert await m.verify_polling_available(h,'synthetic-only')==0 and all('Webhook' not in x[0] or x[0]=='getWebhookInfo' for x in h.calls)
    asyncio.run(run())


@pytest.mark.parametrize('changes',[{'is_bot':False},{'id':True},{'id':9007199254740992},{'username':'invalid/name'},{'username':None}])
def test_invalid_bot_identity_is_not_accepted(changes):
    async def run():
        h=Http();h.responses['getMe'].update(changes)
        with pytest.raises(HTTPException):await subject().verify_bot_identity(h,'synthetic-only')
    asyncio.run(run())


@pytest.mark.parametrize('kind',['wrong_chat','channel','channel_dm','wrong_member','left','kicked','restricted','default_deny'])
def test_unverified_chat_or_missing_send_rights_are_rejected(kind):
    async def run():
        h=Http()
        if kind=='wrong_chat':h.responses['getChat']['id']=-301
        elif kind=='channel':h.responses['getChat']['type']='channel'
        elif kind=='channel_dm':h.responses['getChat']['is_direct_messages']=True
        elif kind=='wrong_member':h.responses['getChatMember']['user']['id']=101
        elif kind in {'left','kicked'}:h.responses['getChatMember']['status']=kind
        elif kind=='restricted':h.responses['getChatMember'].update(status='restricted',is_member=True,can_send_messages=False)
        else:h.responses['getChat']['permissions']['can_send_messages']=False
        with pytest.raises(HTTPException):await subject().verify_chat_access(h,'synthetic-only','100','-300')
    asyncio.run(run())


def test_administrator_and_restricted_rights_override_chat_defaults():
    async def run():
        h=Http();h.responses['getChat']['permissions']={'can_send_messages':False};h.responses['getChatMember']['status']='administrator'
        assert (await subject().verify_chat_access(h,'synthetic-only','100','-300')).can_send_photos
        h.responses['getChatMember'].update(status='restricted',is_member=True,can_send_messages=True,can_send_photos=False)
        assert not (await subject().verify_chat_access(h,'synthetic-only','100','-300')).can_send_photos
    asyncio.run(run())


def test_private_chat_is_verified_without_changing_membership_or_creating_chat():
    async def run():
        h=Http();h.responses['getChat']={'id':300,'type':'private'};chat=await subject().verify_chat_access(h,'synthetic-only','100','300');assert chat.chat_type=='private' and chat.can_send_photos and [x[0] for x in h.calls]==['getChat']
    asyncio.run(run())


def test_existing_webhook_is_preserved_and_blocks_polling():
    async def run():
        h=Http();h.responses['getWebhookInfo']['url']='https://example.invalid/existing'
        with pytest.raises(HTTPException) as e:await subject().verify_polling_available(h,'synthetic-only')
        assert e.value.detail=='TELEGRAM_WEBHOOK_EXISTS' and [x[0] for x in h.calls]==['getWebhookInfo']
    asyncio.run(run())


def test_transport_uses_fixed_host_post_and_no_redirect_or_raw_error():
    calls=[]
    def post(url,body):calls.append((url,body));return {'ok':True,'result':{'id':100}}
    m=subject();h=m.TelegramHttpTransport(post=post);assert h.call('getMe','100:synthetic-only',{})=={'id':100}
    assert calls[0][0]=='https://api.telegram.org/bot100:synthetic-only/getMe' and json.loads(calls[0][1])=={}
    for method,token in [('deleteWebhook','100:synthetic-only'),('getMe','100:bad/../token'),('https://example.invalid','100:synthetic-only')]:
        with pytest.raises(HTTPException):h.call(method,token,{})
    assert len(calls)==1


def test_transport_distinguishes_definite_rejection_from_unknown_without_description():
    m=subject();h=m.TelegramHttpTransport(post=lambda *_:{'ok':False,'error_code':429,'description':'do-not-expose','parameters':{'retry_after':17}})
    with pytest.raises(m.TelegramApiRejected) as e:h.call('getMe','100:synthetic-only',{})
    assert e.value.retry_after==17 and 'do-not-expose' not in str(e.value)
    h=m.TelegramHttpTransport(post=lambda *_:{'ok':False,'error_code':500,'description':'do-not-expose'})
    with pytest.raises(HTTPException) as e:h.call('getMe','100:synthetic-only',{})
    assert e.value.detail=='TELEGRAM_TRANSPORT_FAILED'


def test_default_transport_does_not_accept_redirect_as_success_or_leak_url(monkeypatch):
    from types import SimpleNamespace
    m=subject()
    class Response:
        status=302
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,*_):return b'{"ok":true,"result":{"id":100}}'
    monkeypatch.setattr(m,'build_opener',lambda *_:SimpleNamespace(open=lambda *_,**__:Response()))
    with pytest.raises(HTTPException) as e:m._post('https://api.telegram.org/bot100:synthetic-only/getMe',b'{}')
    assert e.value.detail=='TELEGRAM_TRANSPORT_FAILED'
    assert m._NoRedirect().redirect_request(None,None,302,'redirect',{},'https://example.invalid') is None
