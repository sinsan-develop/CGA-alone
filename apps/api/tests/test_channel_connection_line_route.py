from types import SimpleNamespace
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.api.routes import channel_connection_events as route

def client(monkeypatch,enabled=True,fail=False):
    calls=[]
    async def receive(header,body):
        calls.append((header,body))
        if fail:raise HTTPException(503,'SYNTHETIC_COMMIT_FAILED')
    monkeypatch.setattr(route.settings,'channel_connection_worker_enabled',enabled)
    monkeypatch.setattr(route.settings,'channel_connection_encryption_key','synthetic-only')
    monkeypatch.setitem(route.ADAPTERS,'line',SimpleNamespace(receive=receive))
    app=FastAPI();app.include_router(route.router,prefix='/api/v1');return TestClient(app),calls

def test_disabled_line_receiver_never_executes(monkeypatch):
    c,calls=client(monkeypatch,False);r=c.post('/api/v1/channel-connections/line/events',content=b'{}');assert r.status_code==503 and not calls

def test_line_signature_receives_unmodified_bytes_before_json(monkeypatch):
    c,calls=client(monkeypatch);raw=b'{ "events": [], "destination": "synthetic-only" }';r=c.post('/api/v1/channel-connections/line/events',content=raw,headers={'x-line-signature':'synthetic-signature'})
    assert r.status_code==200 and r.json()=={} and calls==[('synthetic-signature',raw)]

def test_line_commit_failure_is_not_acknowledged_or_retried(monkeypatch):
    c,calls=client(monkeypatch,fail=True);r=c.post('/api/v1/channel-connections/line/events',content=b'{}');assert r.status_code==503 and len(calls)==1

def test_line_large_body_is_rejected_before_adapter(monkeypatch):
    c,calls=client(monkeypatch);r=c.post('/api/v1/channel-connections/line/events',content=b'x'*262145);assert r.status_code==413 and not calls
