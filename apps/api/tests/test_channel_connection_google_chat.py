from importlib import import_module
from copy import deepcopy
import json,time
from urllib.parse import parse_qs
import jwt,pytest
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from tests.test_channel_connection_google_chat_auth import signing


def subject():return import_module('app.services.channel_connection_google_chat')


@pytest.fixture
def credentials(signing):
    key,_=signing;project='cga-qa-project'
    value={'type':'service_account','project_id':project,'private_key_id':'synthetic-key','private_key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),'client_email':'qa@'+project+'.iam.gserviceaccount.com','token_uri':'https://oauth2.googleapis.com/token'}
    return {'service_account_json':json.dumps(value),'project_id':project}


def test_service_assertion_uses_fixed_token_url_and_minimal_scope(credentials,signing):
    m=subject();identity=m.service_account(credentials);calls=[]
    def request(method,url,body,token):calls.append((method,url,body,token));return {'access_token':'synthetic-access','token_type':'Bearer','expires_in':3600}
    value=m.GoogleChatHttpTransport(request=request).oauth(identity);method,url,body,token=calls[0]
    assert method=='POST' and url==m.OAUTH_URL and token is None
    form=parse_qs(body.decode());assert form['grant_type']==['urn:ietf:params:oauth:grant-type:jwt-bearer']
    decoded=jwt.decode(form['assertion'][0],signing[0].public_key(),algorithms=['RS256'],audience=m.OAUTH_URL)
    assert decoded['iss']==identity.client_email and decoded['scope']=='https://www.googleapis.com/auth/chat.bot'
    assert decoded['exp']-decoded['iat']==3600 and 'PRIVATE KEY' not in repr(identity)


@pytest.mark.parametrize('bad',['project','token_uri','email','type','key'])
def test_service_account_configuration_rejects_untrusted_mapping(credentials,bad):
    value=json.loads(credentials['service_account_json'])
    if bad=='project':credentials['project_id']='another-project'
    elif bad=='token_uri':value['token_uri']='https://untrusted.invalid/token'
    elif bad=='email':value['client_email']='not-an-account'
    elif bad=='type':value['type']='authorized_user'
    elif bad=='key':value['private_key']='synthetic-invalid-key'
    credentials['service_account_json']=json.dumps(value)
    with pytest.raises(HTTPException):subject().service_account(credentials)


@pytest.mark.parametrize('bad',[False,True])
def test_space_and_calling_app_membership_are_verified(bad):
    m=subject()
    class Http:
        def get(self,path,token):
            if path=='spaces/QA':return {'name':'spaces/QA','spaceType':'SPACE'}
            assert path=='spaces/QA/members/app';return {'name':'spaces/QA/members/100','state':'JOINED','member':{'name':'users/other' if bad else 'users/100','type':'BOT'}}
    if bad:
        with pytest.raises(HTTPException):m.verify_space(Http(),'synthetic-access','spaces/QA','users/100')
    else:assert m.verify_space(Http(),'synthetic-access','spaces/QA','users/100')=='SPACE'


@pytest.mark.parametrize('value',['https://untrusted.invalid','spaces/../private','spaces/QA?token=x','spaces/QA/threads/other'])
def test_space_resource_cannot_change_host_or_resource_type(value):
    with pytest.raises(HTTPException):subject().resource_name(value,'space')


def test_message_post_uses_fixed_space_thread_and_request_id():
    m=subject();calls=[]
    def request(*args):calls.append(args);return {'name':'spaces/QA/messages/1'}
    http=m.GoogleChatHttpTransport(request=request);request_id='00000000-0000-4000-8000-000000000001'
    http.send('spaces/QA','synthetic-access',{'text':'QA','thread':{'name':'spaces/QA/threads/T'}},request_id,'REPLY_MESSAGE_OR_FAIL')
    assert calls[0][0]=='POST' and calls[0][1].startswith('https://chat.googleapis.com/v1/spaces/QA/messages?') and 'requestId='+request_id in calls[0][1]
    with pytest.raises(HTTPException):http.send('spaces/QA','synthetic-access',{'text':'QA','thread':{'name':'spaces/OTHER/threads/T'}},request_id,None)
    assert len(calls)==1
