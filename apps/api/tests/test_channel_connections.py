from importlib import import_module
from uuid import uuid4
import base64
import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError


def service():
    return import_module("app.services.channel_connections")


def key():
    return base64.urlsafe_b64encode(bytes(range(32))).decode()


def test_credential_roundtrip_is_bound_to_organization_connection_and_key_version():
    s = service()
    org, connection = uuid4(), uuid4()
    encrypted = s.encrypt_credentials({"bot_token": "synthetic-value"}, org, connection, key(), "v1")
    assert "synthetic-value" not in encrypted
    assert s.decrypt_credentials(encrypted, org, connection, key(), "v1") == {"bot_token": "synthetic-value"}
    for other_org, other_connection, version in [(uuid4(), connection, "v1"), (org, uuid4(), "v1"), (org, connection, "v2")]:
        with pytest.raises(HTTPException) as error:
            s.decrypt_credentials(encrypted, other_org, other_connection, key(), version)
        assert error.value.status_code == 503
        assert "synthetic-value" not in str(error.value.detail)


def test_credentials_fail_closed_for_missing_key_corruption_and_masks():
    s = service(); org, connection = uuid4(), uuid4()
    for secret, encryption_key in [({"bot_token": "synthetic-value"}, ""), ({"bot_token": "********"}, key()), ({"bot_token": "등록됨"}, key())]:
        with pytest.raises(HTTPException):
            s.encrypt_credentials(secret, org, connection, encryption_key, "v1")
    with pytest.raises(HTTPException):
        s.decrypt_credentials("broken", org, connection, key(), "v1")


def test_serializer_has_no_ciphertext_or_plaintext_and_reports_unverified():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    s = service()
    row = SimpleNamespace(id=uuid4(), channel_id=uuid4(), bot_id=uuid4(), name="연결A", provider="slack", installation_id="T1", account_id="B1", target_id="C1", desired_state="disconnected", observed_state="disconnected", generation=1, credential_ciphertext="sensitive-ciphertext", credential_key_version="v1", last_error_code=None, last_checked_at=None, lease_expires_at=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    result = s.serialize_connection(row)
    assert result["credentials_registered"] is True
    assert result["identity_verified"] is False
    assert "sensitive-ciphertext" not in str(result)
    assert "credential_ciphertext" not in result


def test_disconnect_is_idempotent_and_fences_prior_generation():
    from types import SimpleNamespace
    s=service()
    a=SimpleNamespace(desired_state="connected", observed_state="connected", generation=3, lease_owner="old", lease_expires_at=None, last_error_code="old")
    b=SimpleNamespace(desired_state="connected", observed_state="connected", generation=7)
    s.disconnect_connection(a)
    assert (a.desired_state,a.observed_state,a.generation,a.lease_owner)==("disconnected","disconnected",4,None)
    s.disconnect_connection(a)
    assert a.generation==4
    assert (b.desired_state,b.generation)==("connected",7)


def test_dto_rejects_empty_identifiers_and_credentials_are_not_repr():
    schema=import_module("app.schemas.channel_connection")
    payload=dict(name="연결A",bot_id=uuid4(),installation_id="T1",account_id="B1",target_id="C1",credentials={"bot_token":"synthetic-value"})
    parsed=schema.ConnectionWrite(**payload)
    assert isinstance(parsed.credentials["bot_token"], SecretStr)
    assert "synthetic-value" not in repr(parsed)
    for name in ("name","installation_id","account_id","target_id"):
        with pytest.raises(ValidationError):schema.ConnectionWrite(**{**payload,name:"  "})


def test_connection_routes_require_authentication_and_never_echo_invalid_secret():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps import get_db, get_current_user
    from types import SimpleNamespace
    router = import_module("app.api.routes.channel_connections").router
    app=FastAPI(); app.include_router(router,prefix="/api/v1")
    app.dependency_overrides[get_db]=lambda: None
    base=f"/api/v1/admin/channels/{uuid4()}/connections"
    with TestClient(app) as client:
        assert client.get(base).status_code==401
        assert client.post(base,json={}).status_code==401
        app.dependency_overrides[get_current_user]=lambda: SimpleNamespace(id=uuid4())
        response=client.post(base,json={"name":"", "credentials":{"bot_token":"synthetic-secret-do-not-return"}})
        assert response.status_code==422
        assert "synthetic-secret" not in response.text


from tests.integration.conftest import test_engine
from tests.integration.test_bot_persistence_regression import migrated_test_database


@pytest.fixture
def connection_api(test_engine, migrated_test_database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app.models import Organization, Group, User, Role, UserRole, AdminChannel, Bot
    from app.api.deps import get_db, get_current_user
    from app.core.config import settings
    from types import SimpleNamespace
    router=import_module("app.api.routes.channel_connections").router
    connection=test_engine.connect(); transaction=connection.begin()
    db=Session(bind=connection,join_transaction_mode="create_savepoint",expire_on_commit=False)
    suffix=uuid4().hex
    org=Organization(code="c01-"+suffix,name="C01 QA"); other=Organization(code="c01-other-"+suffix,name="C01 other")
    db.add_all([org,other]);db.flush()
    group=Group(organization_id=org.id,code="c01",name="C01"); db.add(group);db.flush()
    user=User(organization_id=org.id,group_id=group.id,login_id="c01"+suffix,name="C01",status="active",password_hash="synthetic-only")
    db.add(user);db.flush()
    role=db.scalar(select(Role).where(Role.code=="system_manager"))
    if role is None: role=Role(code="system_manager",name="시스템 관리자");db.add(role);db.flush()
    db.add(UserRole(user_id=user.id,role_id=role.id))
    bot=Bot(organization_id=org.id,group_id=group.id,name="C01 봇",created_by=user.id)
    parent=AdminChannel(organization_id=org.id,code="SLACK",name="Slack",data_json={"provider":"slack"})
    foreign=AdminChannel(organization_id=other.id,code="SLACK",name="Other Slack",data_json={"provider":"slack"})
    db.add_all([bot,parent,foreign]);db.commit()
    app=FastAPI();app.include_router(router,prefix="/api/v1")
    from app.api.routes import admin
    app.include_router(admin.router,prefix="/api/v1")
    app.dependency_overrides[get_db]=lambda: db
    app.dependency_overrides[get_current_user]=lambda: user
    monkeypatch.setattr(settings,"channel_connection_encryption_key",key())
    base=f"/api/v1/admin/channels/{parent.id}/connections"
    payload=dict(name="연결A",bot_id=str(bot.id),installation_id="T1",account_id="B1",target_id="C1",credentials={"bot_token":"synthetic-value"})
    try:
        with TestClient(app) as client: yield SimpleNamespace(client=client,db=db,user=user,org=org,bot=bot,parent=parent,foreign=foreign,base=base,payload=payload)
    finally:
        db.close();transaction.rollback();connection.close()


def test_api_crud_duplicate_mask_and_other_organization(connection_api):
    from sqlalchemy import select
    from app.models import AuditLog
    c=connection_api
    response=c.client.post(c.base,json=c.payload);assert response.status_code==200,response.text
    a=response.json()["data"]["item"];aid=a["id"]
    assert a["observed_state"]=="disconnected" and a["credentials_registered"] is True
    assert "synthetic-value" not in response.text
    assert c.client.post(c.base,json=c.payload).status_code==409
    b=c.client.post(c.base,json={**c.payload,"name":"연결B","target_id":"C2"}).json()["data"]["item"]
    rows=c.client.get(c.base).json()["data"]["items"]
    assert len(rows)==2 and "credential_ciphertext" not in str(rows)
    assert c.client.get(f"/api/v1/admin/channels/{c.foreign.id}/connections").status_code==404
    assert c.client.patch(c.base+"/"+aid,json={**c.payload,"credentials":{"bot_token":"********"}}).status_code==422
    assert c.client.patch(c.base+"/"+aid,json={**c.payload,"name":"수정","credentials":{}}).status_code==200
    checked=c.client.post(c.base+"/"+aid+"/check").json()["data"]
    assert checked["external_verified"] is False and checked["configuration_valid"] is True
    assert c.client.post(c.base+"/"+aid+"/connect").status_code==409
    assert c.client.post(c.base+"/"+aid+"/disconnect").status_code==200
    assert next(x for x in c.client.get(c.base).json()["data"]["items"] if x["id"]==b["id"])["generation"]==1
    assert c.client.delete(c.base+"/"+aid).status_code==200
    assert len(c.client.get(c.base).json()["data"]["items"])==1
    assert c.client.post(c.base,json=c.payload).status_code==200
    audits=list(c.db.scalars(select(AuditLog).where(AuditLog.actor_user_id==c.user.id)))
    assert audits and "synthetic-value" not in str([(a.before_json,a.after_json) for a in audits])


def test_api_rejects_foreign_bot_non_admin_and_connected_edit(connection_api):
    from app.models import UserRole, ChannelConnection
    from sqlalchemy import delete
    c=connection_api
    assert c.client.post(c.base,json={**c.payload,"bot_id":str(uuid4())}).status_code==404
    a=c.client.post(c.base,json=c.payload).json()["data"]["item"]
    row=c.db.get(ChannelConnection,a["id"]);row.desired_state="connected";row.observed_state="connecting";c.db.commit()
    url=c.base+"/"+a["id"]
    assert c.client.patch(url,json=c.payload).status_code==409
    assert c.client.delete(url).status_code==409
    assert c.client.post(url+"/disconnect").status_code==200
    c.db.execute(delete(UserRole).where(UserRole.user_id==c.user.id));c.db.commit()
    assert c.client.get(c.base).status_code==403


def test_credential_missing_key_rolls_back_registration(connection_api,monkeypatch):
    from app.core.config import settings
    c=connection_api;monkeypatch.setattr(settings,"channel_connection_encryption_key","")
    assert c.client.post(c.base,json=c.payload).status_code==503
    assert c.client.get(c.base).json()["data"]["items"]==[]


def test_parent_cannot_delete_or_change_provider_with_live_connections(connection_api):
    c=connection_api
    assert c.client.post(c.base,json=c.payload).status_code==200
    url=f"/api/v1/admin/channels/{c.parent.id}"
    assert c.client.delete(url).status_code==409
    changed={"name":"Slack","provider":"telegram","renderer_type":"telegram","auth_type":"none","auth_config":{},"status":"active"}
    assert c.client.patch(url,json=changed).status_code==409
    assert c.client.patch(url,json={**changed,"provider":"slack","renderer_type":"slack","name":"표시명 수정"}).status_code==200


def test_partial_credential_update_preserves_other_registered_fields(connection_api):
    from app.models import ChannelConnection
    from app.core.config import settings
    c=connection_api
    initial={**c.payload,"credentials":{"bot_token":"old-synthetic","app_token":"app-synthetic"}}
    item=c.client.post(c.base,json=initial).json()["data"]["item"]
    response=c.client.patch(c.base+"/"+item["id"],json={**c.payload,"credentials":{"bot_token":"new-synthetic"}})
    assert response.status_code==200
    row=c.db.get(ChannelConnection,item["id"])
    values=service().decrypt_credentials(row.credential_ciphertext,row.organization_id,row.id,settings.channel_connection_encryption_key,row.credential_key_version)
    assert values=={"bot_token":"new-synthetic","app_token":"app-synthetic"}


def test_database_unique_target_survives_concurrent_registration(test_engine,migrated_test_database):
    from sqlalchemy.orm import Session
    from sqlalchemy import delete,select
    from sqlalchemy.exc import IntegrityError
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models import Organization, Group, Bot, AdminChannel, ChannelConnection
    from pathlib import Path
    import json
    suffix=uuid4().hex
    receipt=Path(__file__).resolve().parents[3]/".local/channel-connections-c01/concurrent-owner.json"
    receipt.parent.mkdir(parents=True,exist_ok=True)
    with Session(test_engine) as db:
        org=Organization(code="c01-race-"+suffix,name="C01 race");db.add(org);db.flush()
        group=Group(organization_id=org.id,code="c01",name="C01");db.add(group);db.flush()
        bot=Bot(organization_id=org.id,group_id=group.id,name="C01 race")
        parents=[AdminChannel(organization_id=org.id,code="C01"+str(i),name="C01",data_json={"provider":"slack"}) for i in range(2)]
        db.add_all([bot,*parents]);db.flush();oid,gid,bid=org.id,group.id,bot.id;pids=[p.id for p in parents]
        receipt.write_text(json.dumps({"org_id":str(oid),"code":org.code}),encoding="utf-8");db.commit()
    barrier=Barrier(2)
    def register(index):
        with Session(test_engine) as db:
            row=ChannelConnection(organization_id=oid,channel_id=pids[index],bot_id=bid,name="C01",provider="slack",installation_id="T1",account_id="B1",target_id="C1")
            db.add(row);barrier.wait(timeout=5)
            try:db.commit();return "ok"
            except IntegrityError:db.rollback();return "duplicate"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(register,range(2)))
        assert sorted(results)==["duplicate","ok"]
        with Session(test_engine) as db:
            rows=list(db.scalars(select(ChannelConnection).where(ChannelConnection.organization_id==oid)))
            assert len(rows)==1
    finally:
        with Session(test_engine) as db:
            org=db.get(Organization,oid);assert org.code=="c01-race-"+suffix
            for model,criterion in [(ChannelConnection,ChannelConnection.organization_id==oid),(AdminChannel,AdminChannel.organization_id==oid),(Bot,Bot.id==bid),(Group,Group.id==gid),(Organization,Organization.id==oid)]:db.execute(delete(model).where(criterion))
            db.commit();assert db.get(Organization,oid) is None
        receipt.unlink()
