from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.api.routes import channel_connection_events as route


def client(monkeypatch,*,enabled=True,fail=False):
    calls=[]
    async def receive(header,body):
        calls.append((header,body))
        if fail:raise HTTPException(503,'SYNTHETIC_COMMIT_FAILED')
    monkeypatch.setattr(route.settings,'channel_connection_worker_enabled',enabled)
    monkeypatch.setattr(route.settings,'channel_connection_encryption_key','synthetic-only')
    monkeypatch.setitem(route.ADAPTERS,'google_chat',SimpleNamespace(receive=receive))
    app=FastAPI();app.include_router(route.router,prefix='/api/v1');return TestClient(app),calls


def test_disabled_receiver_never_reads_provider_or_executes(monkeypatch):
    c,calls=client(monkeypatch,enabled=False);r=c.post('/api/v1/channel-connections/google-chat/events',json={})
    assert r.status_code==503 and calls==[]


def test_empty_ack_is_returned_only_after_committed_receive(monkeypatch):
    c,calls=client(monkeypatch);r=c.post('/api/v1/channel-connections/google-chat/events',json={'type':'MESSAGE'},headers={'Authorization':'Bearer synthetic-only'})
    assert r.status_code==200 and r.json()=={} and calls==[('Bearer synthetic-only',{'type':'MESSAGE'})]


def test_failed_commit_never_returns_success_or_retries(monkeypatch):
    c,calls=client(monkeypatch,fail=True);r=c.post('/api/v1/channel-connections/google-chat/events',json={})
    assert r.status_code==503 and len(calls)==1


@pytest.mark.parametrize('payload,expected',[(b'[]',422),(b'{',422),(b'x'*(256*1024+1),413)],ids=['non-object','bad-json','oversized'])
def test_invalid_or_oversized_body_never_reaches_adapter(monkeypatch,payload,expected):
    c,calls=client(monkeypatch);r=c.post('/api/v1/channel-connections/google-chat/events',content=payload,headers={'Content-Type':'application/json'})
    assert r.status_code==expected and calls==[]
