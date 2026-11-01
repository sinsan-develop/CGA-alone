"""기존 채널 아래 복수 연결 관리. 실제 플랫폼 실행은 adapter 활성화 전 거부한다."""
from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import get_current_user, get_db
from app.api.routes.admin import _require_admin_user, _require_operations_view_user
from app.core.config import settings
from app.core.responses import success_response
from app.models import AdminChannel, AuditLog, Bot, ChannelConnection, Group, Organization, User
from app.schemas.channel_connection import ConnectionWrite
from app.services.channel_connections import PROVIDERS, decrypt_credentials, disconnect_connection, encrypt_credentials, serialize_connection


class CredentialSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                # Pydantic 오류의 input 필드에 원문 Secret이 포함될 수 있다.
                raise HTTPException(422, "INVALID_CONNECTION_INPUT: 연결 설정 입력을 확인하세요.") from None
        return handle


router = APIRouter(prefix="/admin/channels", tags=["channel-connections"], route_class=CredentialSafeRoute)


def _parent(db: Session, user: User, channel_id: UUID, *, write: bool = False):
    (_require_admin_user if write else _require_operations_view_user)(db, user)
    organization = db.get(Organization, user.organization_id)
    if organization is None or organization.deleted_at is not None or organization.status != "active":
        raise HTTPException(403, "ORGANIZATION_INACTIVE")
    query = select(AdminChannel).where(AdminChannel.id == channel_id, AdminChannel.organization_id == user.organization_id, AdminChannel.deleted_at.is_(None))
    if write:
        query = query.with_for_update()
    channel = db.scalar(query)
    if channel is None:
        raise HTTPException(404, "CHANNEL_NOT_FOUND")
    return channel


def _connection(db: Session, user: User, parent, connection_id: UUID):
    row = db.scalar(select(ChannelConnection).where(ChannelConnection.id == connection_id, ChannelConnection.channel_id == parent.id, ChannelConnection.organization_id == user.organization_id, ChannelConnection.deleted_at.is_(None)).with_for_update())
    if row is None:
        raise HTTPException(404, "CONNECTION_NOT_FOUND")
    return row


def _bot(db: Session, user: User, bot_id: UUID):
    bot = db.scalar(select(Bot).join(Group, Group.id == Bot.group_id).where(Bot.id == bot_id, Bot.organization_id == user.organization_id, Bot.deleted_at.is_(None), Bot.status == "active", Group.organization_id == user.organization_id, Group.deleted_at.is_(None), Group.status == "active"))
    if bot is None:
        raise HTTPException(404, "BOT_NOT_FOUND")
    return bot


def _provider(parent):
    provider = str((parent.data_json or {}).get("provider", "")).replace("-", "_")
    if provider not in PROVIDERS:
        raise HTTPException(422, "UNSUPPORTED_CONNECTION_PROVIDER")
    return provider


def _editable(row):
    if row.desired_state != "disconnected" or row.observed_state not in {"disconnected", "error"}:
        raise HTTPException(409, "DISCONNECT_FIRST: 연결 해제 후 수정·삭제하세요.")


def _audit(db, request, user, row, action):
    db.add(AuditLog(actor_user_id=user.id, action_type="admin.connection." + action, target_type="channel_connection", target_id=row.id,
        after_json={"channel_id": str(row.channel_id), "state": row.observed_state, "generation": row.generation}, ip_address=request.client.host if request.client else None))


def _commit(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # 입력·암호문이 들어 있는 driver 오류를 로그/응답에 내보내지 않는다.
        raise HTTPException(409, "DUPLICATE_CONNECTION: 같은 외부 계정·대상이 이미 등록되어 있습니다.") from None


def _apply(row, payload, user):
    if payload.credentials:
        values = {}
        if row.credential_ciphertext:
            values = decrypt_credentials(row.credential_ciphertext, user.organization_id, row.id, settings.channel_connection_encryption_key, row.credential_key_version)
        values.update({key: secret.get_secret_value() for key, secret in payload.credentials.items()})
        encrypted = encrypt_credentials(values, user.organization_id, row.id, settings.channel_connection_encryption_key, settings.channel_connection_key_version)
        row.credential_ciphertext = encrypted
        row.credential_key_version = settings.channel_connection_key_version
    row.name = payload.name
    row.bot_id = payload.bot_id
    row.installation_id = payload.installation_id
    row.account_id = payload.account_id
    row.target_id = payload.target_id
    row.last_checked_at = None
    row.last_error_code = None
    row.updated_by = user.id


@router.get("/{channel_id}/connections")
def list_connections(channel_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent = _parent(db, current_user, channel_id)
    rows = db.scalars(select(ChannelConnection).where(ChannelConnection.channel_id == parent.id, ChannelConnection.organization_id == current_user.organization_id, ChannelConnection.deleted_at.is_(None)).order_by(ChannelConnection.created_at, ChannelConnection.id)).all()
    return success_response(request, {"items": [serialize_connection(row) for row in rows], "total": len(rows)})


@router.get("/{channel_id}/connections/bots")
def list_connection_bots(channel_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _parent(db, current_user, channel_id, write=True)
    rows=db.scalars(select(Bot).join(Group, Group.id == Bot.group_id).where(Bot.organization_id == current_user.organization_id, Bot.deleted_at.is_(None), Bot.status == "active", Group.organization_id == current_user.organization_id, Group.deleted_at.is_(None), Group.status == "active").order_by(Bot.name)).all()
    return success_response(request, {"items": [{"id": str(row.id), "name": row.name} for row in rows]})


@router.post("/{channel_id}/connections")
def create_connection(channel_id: UUID, payload: ConnectionWrite, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True)
    _bot(db,current_user,payload.bot_id)
    row=ChannelConnection(id=uuid4(),organization_id=current_user.organization_id,channel_id=parent.id,provider=_provider(parent),created_by=current_user.id,desired_state="disconnected",observed_state="disconnected",generation=1)
    _apply(row,payload,current_user)
    db.add(row);_audit(db,request,current_user,row,"create");_commit(db)
    return success_response(request,{"item":serialize_connection(row)})


@router.patch("/{channel_id}/connections/{connection_id}")
def update_connection(channel_id: UUID, connection_id: UUID, payload: ConnectionWrite, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True);row=_connection(db,current_user,parent,connection_id)
    _editable(row);_bot(db,current_user,payload.bot_id)
    _apply(row,payload,current_user)
    db.add(row);_audit(db,request,current_user,row,"update");_commit(db)
    return success_response(request,{"item":serialize_connection(row)})


@router.delete("/{channel_id}/connections/{connection_id}")
def delete_connection(channel_id: UUID, connection_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True);row=_connection(db,current_user,parent,connection_id)
    _editable(row);row.deleted_at=datetime.now(timezone.utc);row.updated_by=current_user.id
    _audit(db,request,current_user,row,"delete");_commit(db)
    return success_response(request,{"deleted":True})


@router.post("/{channel_id}/connections/{connection_id}/check")
def check_connection(channel_id: UUID, connection_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True);row=_connection(db,current_user,parent,connection_id)
    _bot(db,current_user,row.bot_id)
    issues=[]
    if parent.status != "active": issues.append("CHANNEL_INACTIVE")
    if not row.credential_ciphertext:
        issues.append("CREDENTIALS_MISSING")
    else:
        decrypt_credentials(row.credential_ciphertext,row.organization_id,row.id,settings.channel_connection_encryption_key,row.credential_key_version)
    row.last_checked_at=datetime.now(timezone.utc)
    _audit(db,request,current_user,row,"check");_commit(db)
    return success_response(request,{"configuration_valid":not issues,"external_verified":False,"issues":issues,"execution_available":False,"item":serialize_connection(row)})


@router.post("/{channel_id}/connections/{connection_id}/connect")
def connect_connection(channel_id: UUID, connection_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True);_connection(db,current_user,parent,connection_id)
    raise HTTPException(409,"CONNECTION_EXECUTION_UNAVAILABLE: 実接속 실행부 준비 전입니다.".replace("実", "실"))


@router.post("/{channel_id}/connections/{connection_id}/disconnect")
def disconnect(channel_id: UUID, connection_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    parent=_parent(db,current_user,channel_id,write=True);row=_connection(db,current_user,parent,connection_id)
    disconnect_connection(row);row.updated_by=current_user.id
    _audit(db,request,current_user,row,"disconnect");_commit(db)
    return success_response(request,{"item":serialize_connection(row)})
