"""PostgreSQL 연결/전달 원자성. 네트워크와 LLM 호출은 이 모듈 밖에 둔다."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.models import AdminChannel, Bot, Group, Organization, ChannelConnection, ChannelInstallationLease, ChannelConnectionDelivery


def clock(db):
    return db.scalar(select(func.clock_timestamp()))


def _connection(db, connection_id, *, lock=True):
    query=select(ChannelConnection).where(ChannelConnection.id==connection_id,ChannelConnection.deleted_at.is_(None)).execution_options(populate_existing=True)
    if lock:query=query.with_for_update()
    row=db.scalar(query)
    if row is None:raise HTTPException(404,"CONNECTION_NOT_FOUND")
    return row


def _installation(db, installation_id, owner, fence):
    row=db.scalar(select(ChannelInstallationLease).where(ChannelInstallationLease.id==installation_id).with_for_update().execution_options(populate_existing=True))
    if row is None or row.owner!=owner or row.fence!=fence or row.lease_expires_at is None or row.lease_expires_at<=clock(db):
        raise HTTPException(409,"INSTALLATION_LEASE_LOST")
    return row


def _target_active(db, row):
    parent=db.get(AdminChannel,row.channel_id);bot=db.get(Bot,row.bot_id)
    org=db.get(Organization,row.organization_id);group=db.get(Group,bot.group_id) if bot else None
    if (row.desired_state!="connected" or parent is None or parent.deleted_at is not None or parent.status!="active"
        or parent.organization_id!=row.organization_id or bot is None or bot.deleted_at is not None or bot.status!="active"
        or bot.organization_id!=row.organization_id or group is None or group.deleted_at is not None or group.status!="active"
        or group.organization_id!=row.organization_id or org is None or org.deleted_at is not None or org.status!="active"):
        raise HTTPException(409,"CONNECTION_TARGET_INACTIVE")


def _scope_matches(row, installation):
    if (row.organization_id,row.provider,row.installation_id,row.account_id)!=(installation.organization_id,installation.provider,installation.installation_id,installation.account_id):
        raise HTTPException(409,"INSTALLATION_SCOPE_MISMATCH")


def claim_installation(db, connection, owner: str, *, ttl_seconds: int):
    if not owner or len(owner)>120 or not 5<=ttl_seconds<=600:raise HTTPException(422,"INVALID_LEASE_INPUT")
    row=_connection(db,connection.id,lock=False)
    _target_active(db,row)
    values=dict(organization_id=row.organization_id,provider=row.provider,installation_id=row.installation_id,account_id=row.account_id)
    db.execute(pg_insert(ChannelInstallationLease).values(id=uuid4(),**values).on_conflict_do_nothing(index_elements=["organization_id","provider","installation_id","account_id"]))
    lease=db.scalar(select(ChannelInstallationLease).filter_by(**values).with_for_update().execution_options(populate_existing=True))
    now=clock(db)
    if lease.owner!=owner and lease.lease_expires_at is not None and lease.lease_expires_at>now:
        return None
    if lease.owner!=owner or lease.lease_expires_at is None or lease.lease_expires_at<=now:
        lease.fence+=1
    lease.owner=owner;lease.lease_expires_at=now+timedelta(seconds=ttl_seconds)
    db.flush();return lease


def activate_connection(db, connection_id, installation_id, owner, fence, verified_identity: dict):
    lease=_installation(db,installation_id,owner,fence)
    row=_connection(db,connection_id);_scope_matches(row,lease);_target_active(db,row)
    if verified_identity.get("installation_id")!=row.installation_id or verified_identity.get("account_id")!=row.account_id:
        raise HTTPException(409,"VERIFIED_IDENTITY_MISMATCH")
    if row.observed_state!="connected" or row.lease_owner!=owner or row.lease_expires_at is None or row.lease_expires_at<=clock(db):
        row.accept_after=clock(db)
    row.observed_state="connected";row.lease_owner=owner;row.lease_expires_at=lease.lease_expires_at;row.last_error_code=None
    db.flush();return row


def _live(db, connection_id, installation_id, owner, fence):
    lease=_installation(db,installation_id,owner,fence)
    row=_connection(db,connection_id);_scope_matches(row,lease);_target_active(db,row)
    if row.observed_state!="connected" or row.lease_owner!=owner or row.lease_expires_at is None or row.lease_expires_at<=clock(db):
        raise HTTPException(409,"CONNECTION_LEASE_LOST")
    return row


def scoped_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event(value):
    fields={"eventId","nativeRoomKey","nativeParticipantKey","participantKind","participantName","roomMode","addressed","message","sourceMessageId","sourceTalkNodeId","choiceIndex","channelId","threadTs","chatId","threadId","spaceName","threadName","timestamp","redelivery","occurredAt"}
    if not isinstance(value,dict) or set(value)-fields:raise HTTPException(422,"INVALID_NATIVE_EVENT")
    if any(not isinstance(value.get(k),str) or not value[k] or len(value[k])>limit for k,limit in [("eventId",512),("nativeRoomKey",2048),("nativeParticipantKey",512),("message",4000)]):
        raise HTTPException(422,"INVALID_NATIVE_EVENT")
    if value.get("participantKind") not in {"user","bot"} or value.get("roomMode") not in {"private","shared"} or not isinstance(value.get("addressed"),bool):
        raise HTTPException(422,"INVALID_NATIVE_EVENT")
    if "participantName" in value and (not isinstance(value["participantName"],str) or not value["participantName"] or len(value["participantName"])>120):
        raise HTTPException(422,"INVALID_NATIVE_EVENT")
    immutable={k:v for k,v in value.items() if k not in {"participantName","timestamp","redelivery","occurredAt"}}
    return scoped_hash(json.dumps(immutable,ensure_ascii=False,sort_keys=True,separators=(",",":")))


def accept_delivery(db, connection_id, installation_id, owner, fence, event: dict):
    row=_live(db,connection_id,installation_id,owner,fence)
    request_hash=_event(event);key=scoped_hash(event["eventId"])
    existing=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.event_key==key).with_for_update())
    if existing is not None:
        if existing.request_hash!=request_hash:raise HTTPException(409,"NATIVE_EVENT_CONFLICT")
        return existing
    status,error="received",None
    if event.get("occurredAt") is not None:
        try:
            occurred=datetime.fromisoformat(event["occurredAt"])
            if occurred.tzinfo is None:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(422,"INVALID_NATIVE_EVENT_TIME") from None
        if row.accept_after is not None and occurred<row.accept_after:status,error="cancelled","BEFORE_CONNECTION"
    delivery=ChannelConnectionDelivery(organization_id=row.organization_id,connection_id=row.id,generation=row.generation,installation_fence=fence,
        native_room_hash=scoped_hash(event["nativeRoomKey"]),native_actor_hash=scoped_hash(event["nativeParticipantKey"]),event_key=key,request_hash=request_hash,status=status,event_json=event,result_json={},provider_message_ids=[],last_error_code=error)
    db.add(delivery);db.flush();return delivery


def claim_delivery(db, connection_id, installation_id, owner, fence, *, deadline_seconds: int):
    if not 5<=deadline_seconds<=7200:raise HTTPException(422,"INVALID_EXECUTION_DEADLINE")
    row=_live(db,connection_id,installation_id,owner,fence)
    deliveries=db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==row.id,ChannelConnectionDelivery.status.in_(["received","executing","ready","sending","unknown"])).order_by(ChannelConnectionDelivery.created_at,ChannelConnectionDelivery.id).with_for_update(skip_locked=True).execution_options(populate_existing=True)).all()
    blocked={item.native_room_hash for item in deliveries if item.generation==row.generation and item.status in {"executing","ready","sending","unknown"}}
    for item in deliveries:
        if item.status!="received":continue
        if item.generation!=row.generation:
            item.status="cancelled";item.last_error_code="CONNECTION_CHANGED";continue
        if item.native_room_hash in blocked:continue
        item.installation_fence=fence;item.status="executing";item.execution_claim_id=str(uuid4());item.deadline_at=clock(db)+timedelta(seconds=deadline_seconds)
        db.flush();return item
    db.flush();return None


def _delivery(db, delivery_id):
    row=db.scalar(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.id==delivery_id).with_for_update().execution_options(populate_existing=True))
    if row is None:raise HTTPException(404,"CONNECTION_DELIVERY_NOT_FOUND")
    return row


def _valid_claim(value) -> bool:
    if not isinstance(value,str):return False
    try:return str(UUID(value))==value
    except ValueError:return False


def mark_ready(db, delivery_id, result: dict, execution_claim_id: str):
    item=_delivery(db,delivery_id)
    if not _valid_claim(execution_claim_id) or item.execution_claim_id!=execution_claim_id or item.send_claim_id is not None or item.status not in {"executing","unknown"}:
        raise HTTPException(409,"EXECUTION_CLAIM_LOST")
    item.result_json=result;item.status="ready";item.deadline_at=None;item.last_error_code=None
    db.flush();return item


def claim_send(db, delivery_id, installation_id, owner, fence, *, deadline_seconds: int=30):
    initial=db.get(ChannelConnectionDelivery,delivery_id)
    if initial is None:raise HTTPException(404,"CONNECTION_DELIVERY_NOT_FOUND")
    try:
        connection=_live(db,initial.connection_id,installation_id,owner,fence)
    except HTTPException:
        connection=db.get(ChannelConnection,initial.connection_id)
        if connection is not None and (connection.desired_state!="connected" or connection.generation!=initial.generation):
            item=_delivery(db,delivery_id)
            if item.status=="ready":item.status="cancelled";item.last_error_code="CONNECTION_CHANGED";db.flush()
        return None
    item=_delivery(db,delivery_id)
    if item.status!="ready":return None
    if item.generation!=connection.generation:
        item.status="cancelled";item.last_error_code="CONNECTION_CHANGED";db.flush();return None
    item.status="sending";item.installation_fence=fence;item.send_claim_id=str(uuid4());item.deadline_at=clock(db)+timedelta(seconds=deadline_seconds)
    db.flush();return item.send_claim_id


def mark_unknown(db, delivery_id, send_claim_id, code: str):
    if not re.fullmatch(r"[A-Z0-9_]{1,80}",code):raise HTTPException(422,"INVALID_DELIVERY_ERROR_CODE")
    item=_delivery(db,delivery_id)
    if not _valid_claim(send_claim_id) or item.send_claim_id!=send_claim_id or item.status!="sending":return False
    item.status="unknown";item.last_error_code=code;db.flush();return True


def record_sent(db, delivery_id, send_claim_id, provider_message_ids: list[str]):
    item=_delivery(db,delivery_id)
    if not _valid_claim(send_claim_id) or item.send_claim_id!=send_claim_id or item.status not in {"sending","unknown"}:return False
    if not isinstance(provider_message_ids,list) or len(provider_message_ids)>20 or any(not isinstance(value,str) or len(value)>512 for value in provider_message_ids):
        raise HTTPException(422,"INVALID_DELIVERY_RECEIPT")
    item.status="sent";item.provider_message_ids=provider_message_ids;item.deadline_at=None;item.last_error_code=None;db.flush();return True


def recover_expired(db, connection_id):
    _connection(db,connection_id)
    rows=db.scalars(select(ChannelConnectionDelivery).where(ChannelConnectionDelivery.connection_id==connection_id,ChannelConnectionDelivery.status.in_(["executing","sending"]),ChannelConnectionDelivery.deadline_at<clock(db)).with_for_update(skip_locked=True)).all()
    for item in rows:
        item.last_error_code="EXECUTION_UNKNOWN" if item.status=="executing" else "DELIVERY_UNKNOWN";item.status="unknown"
    db.flush();return len(rows)
