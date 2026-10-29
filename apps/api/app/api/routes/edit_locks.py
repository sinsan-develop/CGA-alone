from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.responses import success_response
from app.models import AuditLog, Bot, BotVersion, EditLock, Role, User, UserRole
from app.schemas.edit_lock import EditLockArea, EditLockReleaseRequest, EditLockTargetRequest


router = APIRouter(prefix="/edit-locks", tags=["edit-locks"])

LOCK_TTL_SECONDS = 120
FORCE_RELEASE_ROLE_CODES = {"system_manager", "it_admin"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(now: datetime) -> datetime:
    return now + timedelta(seconds=LOCK_TTL_SECONDS)


def _normalize_area(area: str) -> str:
    return "dialog" if area in {"start", "flow"} else area


def _serialize_lock(lock: EditLock, now: datetime | None = None) -> dict[str, object]:
    current_time = now or _now()
    active = lock.released_at is None and lock.expires_at > current_time
    return {
        "lock_id": str(lock.id),
        "bot_id": str(lock.bot_id),
        "version_id": str(lock.version_id),
        "dialog_id": lock.dialog_id,
        "area": lock.area,
        "owner": {
            "user_id": str(lock.owner_user_id),
            "login_id": lock.owner_login_id,
            "name": lock.owner_name,
        },
        "expires_at": lock.expires_at.isoformat(),
        "last_seen_at": lock.last_seen_at.isoformat(),
        "released_at": lock.released_at.isoformat() if lock.released_at else None,
        "active": active,
    }


def _write_edit_lock_audit(
    db: Session,
    request: Request,
    current_user: User,
    action_type: str,
    lock: EditLock,
    after_json: dict[str, object],
) -> None:
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type=action_type,
            target_type="edit_lock",
            target_id=lock.id,
            before_json=None,
            after_json=after_json,
            ip_address=request.client.host if request.client else None,
        )
    )


def _assert_target_access(db: Session, payload: EditLockTargetRequest, current_user: User) -> tuple[Bot, BotVersion]:
    if current_user.group_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="그룹이 지정된 사용자만 봇에 접근할 수 있습니다.")

    bot = db.scalar(
        select(Bot).where(
            Bot.id == payload.bot_id,
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == current_user.group_id,
            Bot.created_by == current_user.id,
            Bot.deleted_at.is_(None),
        )
    )
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="봇을 찾을 수 없습니다.")

    version = db.scalar(
        select(BotVersion).where(
            BotVersion.id == payload.version_id,
            BotVersion.bot_id == bot.id,
            BotVersion.deleted_at.is_(None),
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="버전을 찾을 수 없습니다.")

    return bot, version


def _require_force_release_user(db: Session, current_user: User) -> None:
    role_codes = set(
        db.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == current_user.id, Role.deleted_at.is_(None))
        ).all()
    )
    if not role_codes.intersection(FORCE_RELEASE_ROLE_CODES):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="편집 잠금 강제 해제 권한이 없습니다.")


def _find_lock_by_target(db: Session, payload: EditLockTargetRequest) -> EditLock | None:
    return db.scalar(
        select(EditLock).where(
            EditLock.bot_id == payload.bot_id,
            EditLock.version_id == payload.version_id,
            EditLock.dialog_id == payload.dialog_id,
            EditLock.area == _normalize_area(payload.area),
        )
    )


def _find_release_lock(db: Session, payload: EditLockReleaseRequest, current_user: User) -> EditLock | None:
    if current_user.group_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="그룹이 지정된 사용자만 봇에 접근할 수 있습니다.")
    owned_bot_ids = select(Bot.id).where(
        Bot.organization_id == current_user.organization_id,
        Bot.group_id == current_user.group_id,
        Bot.created_by == current_user.id,
        Bot.deleted_at.is_(None),
    )
    if payload.lock_id is not None:
        return db.scalar(
            select(EditLock).where(
                EditLock.id == payload.lock_id,
                EditLock.bot_id.in_(owned_bot_ids),
                EditLock.organization_id == current_user.organization_id,
                EditLock.group_id == current_user.group_id,
            )
        )
    if not payload.bot_id or not payload.version_id or not payload.dialog_id or not payload.area:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="lock_id 또는 잠금 대상 정보가 필요합니다.")
    return db.scalar(
        select(EditLock).where(
            EditLock.bot_id == payload.bot_id,
            EditLock.bot_id.in_(owned_bot_ids),
            EditLock.version_id == payload.version_id,
            EditLock.dialog_id == payload.dialog_id,
            EditLock.area == _normalize_area(payload.area),
            EditLock.organization_id == current_user.organization_id,
            EditLock.group_id == current_user.group_id,
        )
    )


@router.post("/acquire")
def acquire_edit_lock(
    payload: EditLockTargetRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, _version = _assert_target_access(db, payload, current_user)
    now = _now()
    expires_at = _expires_at(now)
    lock = _find_lock_by_target(db, payload)

    if lock is not None and lock.released_at is None and lock.expires_at > now and lock.owner_user_id != current_user.id:
        _write_edit_lock_audit(
            db,
            request,
            current_user,
            "edit_lock.conflict",
            lock,
            {
                "reason": "locked_by_other",
                "requested_area": _normalize_area(payload.area),
                "requested_dialog_id": payload.dialog_id,
                "owner_login_id": lock.owner_login_id,
                "owner_name": lock.owner_name,
                "expires_at": lock.expires_at.isoformat(),
            },
        )
        db.commit()
        return success_response(
            request,
            {
                "status": "locked_by_other",
                "mode": "view_only_available",
                "lock": _serialize_lock(lock, now),
            },
        )

    if lock is None:
        lock = EditLock(
            organization_id=bot.organization_id,
            group_id=bot.group_id,
            bot_id=payload.bot_id,
            version_id=payload.version_id,
            dialog_id=payload.dialog_id,
            area=_normalize_area(payload.area),
            owner_user_id=current_user.id,
            owner_login_id=current_user.login_id,
            owner_name=current_user.name,
            expires_at=expires_at,
            last_seen_at=now,
            released_at=None,
        )
    else:
        lock.owner_user_id = current_user.id
        lock.owner_login_id = current_user.login_id
        lock.owner_name = current_user.name
        lock.expires_at = expires_at
        lock.last_seen_at = now
        lock.released_at = None

    db.add(lock)
    db.commit()
    db.refresh(lock)
    return success_response(
        request,
        {
            "status": "locked_by_me",
            "mode": "edit",
            "lock": _serialize_lock(lock, now),
        },
    )


@router.post("/heartbeat")
def heartbeat_edit_lock(
    payload: EditLockReleaseRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    lock = _find_release_lock(db, payload, current_user)
    if lock is None or lock.owner_user_id != current_user.id or lock.released_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유효한 잠금을 찾을 수 없습니다.")

    now = _now()
    lock.last_seen_at = now
    lock.expires_at = _expires_at(now)
    db.add(lock)
    db.commit()
    db.refresh(lock)
    return success_response(request, {"status": "locked_by_me", "mode": "edit", "lock": _serialize_lock(lock, now)})


@router.post("/release")
def release_edit_lock(
    payload: EditLockReleaseRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    lock = _find_release_lock(db, payload, current_user)
    if lock is None:
        return success_response(request, {"status": "not_found", "released": False})
    if lock.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="다른 사용자의 잠금은 해제할 수 없습니다.")

    now = _now()
    lock.released_at = now
    lock.last_seen_at = now
    db.add(lock)
    db.commit()
    db.refresh(lock)
    return success_response(request, {"status": "released", "released": True, "lock": _serialize_lock(lock, now)})


@router.post("/force-release")
def force_release_edit_lock(
    payload: EditLockReleaseRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_force_release_user(db, current_user)
    lock = _find_release_lock(db, payload, current_user)
    if lock is None:
        return success_response(request, {"status": "not_found", "released": False})

    now = _now()
    lock.released_at = now
    lock.last_seen_at = now
    db.add(lock)
    _write_edit_lock_audit(
        db,
        request,
        current_user,
        "edit_lock.force_release",
        lock,
        {
            "reason": "admin_force_release",
            "owner_login_id": lock.owner_login_id,
            "owner_name": lock.owner_name,
            "dialog_id": lock.dialog_id,
            "area": lock.area,
            "released_at": now.isoformat(),
        },
    )
    db.commit()
    db.refresh(lock)
    return success_response(request, {"status": "force_released", "released": True, "lock": _serialize_lock(lock, now)})


@router.get("/status")
def get_edit_lock_status(
    bot_id: UUID,
    version_id: UUID,
    dialog_id: str,
    area: EditLockArea,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    payload = EditLockTargetRequest(bot_id=bot_id, version_id=version_id, dialog_id=dialog_id, area=area)
    _assert_target_access(db, payload, current_user)
    lock = _find_lock_by_target(db, payload)
    now = _now()
    if lock is None or lock.released_at is not None or lock.expires_at <= now:
        return success_response(request, {"status": "available", "mode": "edit", "lock": None})
    status_value = "locked_by_me" if lock.owner_user_id == current_user.id else "locked_by_other"
    mode = "edit" if status_value == "locked_by_me" else "view_only_available"
    return success_response(request, {"status": status_value, "mode": mode, "lock": _serialize_lock(lock, now)})
