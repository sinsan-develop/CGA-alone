import asyncio,json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from tests.test_channel_connection_google_chat import credentials
from tests.test_channel_connection_google_chat_auth import signing


class SupportedHttp:
    def __init__(self,project=None):self.calls=[];self.metadata=project or {'projectId':'cga-qa-project','projectNumber':'1234567890','lifecycleState':'ACTIVE'}
    def oauth(self,identity):return {'access_token':'synthetic-only','token_type':'Bearer','expires_in':3600}
    def get_project(self,project_id,token):self.calls.append(('project',project_id));return self.metadata
    def get(self,path,token):
        self.calls.append(('chat',path))
        if '/members/' in path:
            from app.services.channel_connection_google_chat import GoogleChatApiRejected
            raise GoogleChatApiRejected(403)
        return {'name':path,'spaceType':'SPACE'}


def spec():return SimpleNamespace(id=uuid4(),organization_id=uuid4(),provider='google_chat',generation=1,installation_id='1234567890',account_id='users/100',target_id='spaces/QA')


def test_supported_project_and_space_reads_replace_unsupported_self_membership(credentials,monkeypatch):
    from app.services.channel_connection_google_chat import GoogleChatAdapter
    h=SupportedHttp();prepared=[]
    def prepare(self,spec,kind,project_id):prepared.append((kind,project_id));return True
    monkeypatch.setattr(GoogleChatAdapter,'_prepare',prepare)
    async def run():
        target=await GoogleChatAdapter(None,owner='synthetic-worker',http=h).verify(spec(),credentials)
        assert target.listener_scope=='google-chat-project:cga-qa-project' and target.transport.project_id=='cga-qa-project'
    asyncio.run(run());assert h.calls==[('project','cga-qa-project'),('chat','spaces/QA')] and prepared==[('SPACE','cga-qa-project')]


@pytest.mark.parametrize('change',[{'projectId':'another-project'},{'projectNumber':'9876543210'},{'projectNumber':1234567890},{'projectNumber':None},{'lifecycleState':'DELETE_REQUESTED'}])
def test_unverified_project_cannot_prepare_or_claim_organization_scope(credentials,monkeypatch,change):
    from app.services.channel_connection_google_chat import GoogleChatAdapter
    metadata={'projectId':'cga-qa-project','projectNumber':'1234567890','lifecycleState':'ACTIVE',**change};h=SupportedHttp(metadata)
    def forbidden(*_):raise AssertionError('unverified identity cannot prepare')
    monkeypatch.setattr(GoogleChatAdapter,'_prepare',forbidden)
    async def run():
        with pytest.raises(HTTPException,match='GOOGLE_CHAT_PROJECT_VERIFICATION_FAILED'):await GoogleChatAdapter(None,owner='synthetic-worker',http=h).verify(spec(),credentials)
    asyncio.run(run());assert h.calls==[('project','cga-qa-project')]


def test_project_metadata_permission_failure_is_not_connected(credentials,monkeypatch):
    from app.services.channel_connection_google_chat import GoogleChatAdapter,GoogleChatApiRejected
    h=SupportedHttp()
    def denied(*_):raise GoogleChatApiRejected(403)
    h.get_project=denied
    async def run():
        with pytest.raises(HTTPException,match='GOOGLE_CHAT_PROJECT_VERIFICATION_FAILED'):await GoogleChatAdapter(None,owner='synthetic-worker',http=h).verify(spec(),credentials)
    asyncio.run(run())


def test_project_transport_uses_fixed_read_only_resource_and_minimal_scopes(credentials):
    import jwt
    from urllib.parse import parse_qs
    from app.services.channel_connection_google_chat import GoogleChatHttpTransport,service_account
    calls=[]
    def request(*args):calls.append(args);return {}
    h=GoogleChatHttpTransport(request=request);h.oauth(service_account(credentials));h.get_project('cga-qa-project','synthetic-only')
    claims=jwt.decode(parse_qs(calls[0][2].decode())['assertion'][0],options={'verify_signature':False})
    assert set(claims['scope'].split())=={'https://www.googleapis.com/auth/chat.bot','https://www.googleapis.com/auth/cloudplatformprojects.readonly'}
    assert calls[1][:3]==('GET','https://cloudresourcemanager.googleapis.com/v1/projects/cga-qa-project',None)
