from importlib import import_module
from uuid import UUID,uuid4
from datetime import timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func
from tests.test_channel_connections import connection_api,test_engine,migrated_test_database


def runtime():return import_module("app.services.channel_connection_state")


@pytest.fixture
def active_connection(connection_api):
    from app.models import ChannelConnection
    c=connection_api
    item=c.client.post(c.base,json=c.payload).json()["data"]["item"]
    row=c.db.get(ChannelConnection,UUID(item["id"]));row.desired_state="connected";row.observed_state="connecting";c.db.commit()
    return c,row


def test_installation_has_single_owner_and_shared_targets_reuse_same_lease(active_connection):
    c,row=active_connection;s=runtime()
    first=s.claim_installation(c.db,row,"worker-a",ttl_seconds=30);c.db.commit()
    assert first.owner=="worker-a" and first.fence==1
    assert s.claim_installation(c.db,row,"worker-b",ttl_seconds=30) is None
    c.db.commit()
    from app.models import ChannelConnection
    other=c.client.post(c.base,json={**c.payload,"name":"두 번째 대상","target_id":"C2"}).json()["data"]["item"]
    row_b=c.db.get(ChannelConnection,UUID(other["id"]));row_b.desired_state="connected";row_b.observed_state="connecting";c.db.commit()
    second=s.claim_installation(c.db,row_b,"worker-a",ttl_seconds=30);c.db.commit()
    assert second.id==first.id and second.fence==1
    first.lease_expires_at=c.db.scalar(select(func.clock_timestamp()))-timedelta(seconds=1);c.db.commit()
    takeover=s.claim_installation(c.db,row,"worker-b",ttl_seconds=30);c.db.commit()
    assert takeover.id==first.id and takeover.fence==2


def test_activation_requires_matching_verified_installation_and_current_lease(active_connection):
    c,row=active_connection;s=runtime();lease=s.claim_installation(c.db,row,"worker-a",ttl_seconds=30);c.db.commit()
    with pytest.raises(HTTPException):s.activate_connection(c.db,row.id,lease.id,"worker-a",lease.fence,{"installation_id":"wrong","account_id":row.account_id})
    s.activate_connection(c.db,row.id,lease.id,"worker-a",lease.fence,{"installation_id":row.installation_id,"account_id":row.account_id});c.db.commit()
    assert row.observed_state=="connected" and row.lease_owner=="worker-a"


def ready(c,row):
    s=runtime();lease=s.claim_installation(c.db,row,"worker-a",ttl_seconds=60);c.db.commit()
    s.activate_connection(c.db,row.id,lease.id,"worker-a",lease.fence,{"installation_id":row.installation_id,"account_id":row.account_id});c.db.commit()
    return lease


def event(text="질문"):
    return {"eventId":"native-event-a","nativeRoomKey":"native-room-a","nativeParticipantKey":"native-user-a","participantKind":"user","participantName":"질문자","roomMode":"shared","addressed":True,"message":text}


def test_duplicate_event_is_returned_and_conflicting_payload_is_rejected(active_connection):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    one=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    two=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    assert one.id==two.id and one.status=="received"
    with pytest.raises(HTTPException):s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event("다른 질문"))


def test_disconnect_blocks_stale_execution_and_preserves_prepared_result(active_connection):
    from app.services.channel_connections import disconnect_connection
    c,row=active_connection;s=runtime();lease=ready(c,row)
    delivery=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    claimed=s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60);c.db.commit()
    assert claimed.id==delivery.id and claimed.status=="executing"
    disconnect_connection(row);c.db.commit()
    s.mark_ready(c.db,delivery.id,{"botMessages":[{"text":"생성된 답변"}]},delivery.execution_claim_id);c.db.commit()
    assert delivery.result_json["botMessages"][0]["text"]=="생성된 답변"
    assert s.claim_send(c.db,delivery.id,lease.id,"worker-a",lease.fence) is None
    c.db.commit();assert delivery.status=="cancelled"


def test_sending_timeout_is_unknown_and_is_never_resubmitted(active_connection):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    delivery=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60);c.db.commit()
    s.mark_ready(c.db,delivery.id,{"botMessages":[{"text":"답변"}]},delivery.execution_claim_id);c.db.commit()
    claim=s.claim_send(c.db,delivery.id,lease.id,"worker-a",lease.fence);c.db.commit();assert claim
    s.mark_unknown(c.db,delivery.id,claim,"PROVIDER_TIMEOUT");c.db.commit()
    assert delivery.status=="unknown"
    assert s.claim_send(c.db,delivery.id,lease.id,"worker-a",lease.fence) is None
    s.record_sent(c.db,delivery.id,claim,["native-receipt-1"]);c.db.commit()
    assert delivery.status=="sent" and delivery.provider_message_ids==["native-receipt-1"]


def test_one_room_is_serialized_and_other_rooms_can_progress(active_connection):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    for ev in [event(),{**event(),"eventId":"second"},{**event(),"eventId":"other-room","nativeRoomKey":"native-room-b"}]:
        s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,ev);c.db.commit()
    a=s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60);c.db.commit()
    b=s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60);c.db.commit()
    assert a.native_room_hash!=b.native_room_hash
    assert s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60) is None


@pytest.mark.parametrize("phase",["executing","sending"])
def test_expired_executing_and_sending_recover_to_unknown_without_retry(active_connection,phase):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    one=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60);c.db.commit()
    if phase=="sending":
        s.mark_ready(c.db,one.id,{"botMessages":[{"text":"완료"}]},one.execution_claim_id);c.db.commit()
        assert s.claim_send(c.db,one.id,lease.id,"worker-a",lease.fence);c.db.commit()
    one.deadline_at=c.db.scalar(select(func.clock_timestamp()))-timedelta(seconds=1);c.db.commit()
    assert s.recover_expired(c.db,row.id)==1;c.db.commit()
    assert one.status=="unknown"
    assert s.claim_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,deadline_seconds=60) is None


def test_missing_attempt_proof_cannot_promote_unknown_to_ready_or_sent(active_connection):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    delivery=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event());c.db.commit()
    delivery.status="unknown";delivery.execution_claim_id=None;c.db.commit()
    with pytest.raises(HTTPException):s.mark_ready(c.db,delivery.id,{"botMessages":[]},None)
    assert s.record_sent(c.db,delivery.id,None,[]) is False
    c.db.commit();assert delivery.status=="unknown"


def test_delivery_before_connection_is_not_replayed_and_redelivery_metadata_is_idempotent(active_connection):
    c,row=active_connection;s=runtime();lease=ready(c,row)
    old={**event(),"occurredAt":(row.accept_after-timedelta(seconds=1)).isoformat()}
    one=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,old);c.db.commit()
    assert one.status=="cancelled" and one.last_error_code=="BEFORE_CONNECTION"
    new={**event(),"eventId":"new-event","redelivery":False}
    two=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,new);c.db.commit()
    repeated=s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,{**new,"redelivery":True});c.db.commit()
    assert two.id==repeated.id


def test_other_scope_and_expired_lease_cannot_activate_or_send(active_connection):
    from app.models import ChannelInstallationLease
    c,row=active_connection;s=runtime();lease=ready(c,row)
    foreign=ChannelInstallationLease(listener_scope_hash=lease.listener_scope_hash,organization_id=c.foreign.organization_id,provider=row.provider,installation_id=row.installation_id,account_id=row.account_id,owner="worker-a",fence=1,lease_expires_at=s.clock(c.db)+timedelta(seconds=30))
    c.db.add(foreign);c.db.commit()
    with pytest.raises(HTTPException):s.activate_connection(c.db,row.id,foreign.id,"worker-a",1,{"installation_id":row.installation_id,"account_id":row.account_id})
    lease.lease_expires_at=s.clock(c.db)-timedelta(seconds=1);c.db.commit()
    with pytest.raises(HTTPException):s.accept_delivery(c.db,row.id,lease.id,"worker-a",lease.fence,event())


def test_verified_app_listener_is_shared_across_native_workspaces_and_disconnect_is_independent(active_connection):
    from app.models import ChannelConnection
    from app.services.channel_connections import disconnect_connection
    c,a=active_connection;s=runtime();scope="slack-app:AQA001"
    second=c.client.post(c.base,json={**c.payload,"name":"다른워크스페이스","installation_id":"T2","account_id":"B2","target_id":"C2"}).json()["data"]["item"]
    b=c.db.get(ChannelConnection,UUID(second["id"]));b.desired_state="connected";b.observed_state="connecting";c.db.commit()
    one=s.claim_installation(c.db,a,"worker-a",ttl_seconds=60,listener_scope=scope);c.db.commit()
    two=s.claim_installation(c.db,b,"worker-a",ttl_seconds=60,listener_scope=scope);c.db.commit()
    assert one.id==two.id
    for connection in [a,b]:
        s.activate_connection(c.db,connection.id,one.id,"worker-a",one.fence,{"installation_id":connection.installation_id,"account_id":connection.account_id,"listener_scope":scope});c.db.commit()
    disconnect_connection(a);c.db.commit()
    assert s.accept_delivery(c.db,b.id,one.id,"worker-a",one.fence,event()).connection_id==b.id;c.db.commit()
    assert s.claim_delivery(c.db,b.id,one.id,"worker-a",one.fence,deadline_seconds=60) is not None
    c.db.commit()
    with pytest.raises(HTTPException):s.accept_delivery(c.db,a.id,one.id,"worker-a",one.fence,event())


def test_verified_listener_scope_cannot_attach_to_another_app(active_connection):
    c,row=active_connection;s=runtime()
    lease=s.claim_installation(c.db,row,"worker-a",ttl_seconds=60,listener_scope="slack-app:A1");c.db.commit()
    with pytest.raises(HTTPException):s.activate_connection(c.db,row.id,lease.id,"worker-a",lease.fence,{"installation_id":row.installation_id,"account_id":row.account_id,"listener_scope":"slack-app:A2"})
