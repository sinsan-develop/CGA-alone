from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes.bots import create_bot
from app.api.routes.edit_locks import (
    _find_release_lock, acquire_edit_lock, force_release_edit_lock,
    get_edit_lock_status, heartbeat_edit_lock,
)
from app.models import BotVersion, EditLock, Role, UserRole
from app.schemas.bot import BotCreateRequest
from app.schemas.edit_lock import EditLockReleaseRequest, EditLockTargetRequest
from tests.integration.test_bot_persistence_regression import _request
from tests.cga_alone.test_owner_scope import other_creator


@pytest.fixture
def target(regression_actor, db_session):
    created = create_bot(BotCreateRequest(name="잠금 소유 확인"), _request(), regression_actor, db_session)
    bot_id = UUID(created["data"]["id"])
    version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == bot_id))
    return EditLockTargetRequest(bot_id=bot_id, version_id=version.id, dialog_id="s02-qa-dialog", area="flow")


def test_other_creator_cannot_acquire_edit_lock(target, other_creator, db_session):
    with pytest.raises(HTTPException) as denied:
        acquire_edit_lock(target, _request(), other_creator, db_session)
    assert denied.value.status_code == 404


def test_other_creator_cannot_read_edit_lock(target, other_creator, db_session):
    with pytest.raises(HTTPException) as denied:
        get_edit_lock_status(target.bot_id, target.version_id, target.dialog_id, target.area, _request(), other_creator, db_session)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("by_id", [True, False])
def test_foreign_bot_lock_is_hidden_from_release_lookup(target, by_id, regression_actor, other_creator, db_session):
    acquired = acquire_edit_lock(target, _request(), regression_actor, db_session)
    lock_id = UUID(acquired["data"]["lock"]["lock_id"])
    payload = EditLockReleaseRequest(lock_id=lock_id) if by_id else EditLockReleaseRequest(**target.model_dump())
    assert _find_release_lock(db_session, payload, other_creator) is None


def test_privileged_other_creator_cannot_force_release(target, regression_actor, other_creator, db_session):
    role = db_session.scalar(select(Role).where(Role.code == "it_admin"))
    if role is None:
        role = Role(code="it_admin", name="QA 역할")
        db_session.add(role)
        db_session.flush()
    db_session.add(UserRole(user_id=other_creator.id, role_id=role.id))
    db_session.flush()
    acquired = acquire_edit_lock(target, _request(), regression_actor, db_session)
    lock_id = UUID(acquired["data"]["lock"]["lock_id"])
    result = force_release_edit_lock(EditLockReleaseRequest(lock_id=lock_id), _request(), other_creator, db_session)
    assert result["data"]["released"] is False
    assert db_session.get(EditLock, lock_id).released_at is None


def test_old_foreign_bot_lock_cannot_be_renewed(target, regression_actor, other_creator, db_session):
    acquired = acquire_edit_lock(target, _request(), regression_actor, db_session)
    lock_id = UUID(acquired["data"]["lock"]["lock_id"])
    lock = db_session.get(EditLock, lock_id)
    lock.owner_user_id = other_creator.id  # 수정 이전 API가 만든 타인 봇 잠금 재현
    lock.owner_login_id = other_creator.login_id
    db_session.flush()
    with pytest.raises(HTTPException) as denied:
        heartbeat_edit_lock(EditLockReleaseRequest(lock_id=lock_id), _request(), other_creator, db_session)
    assert denied.value.status_code == 404


def test_other_creator_release_does_not_disclose_lock(target, regression_actor, other_creator, db_session):
    from app.api.routes.edit_locks import release_edit_lock
    acquired = acquire_edit_lock(target, _request(), regression_actor, db_session)
    lock_id = UUID(acquired["data"]["lock"]["lock_id"])
    response = release_edit_lock(EditLockReleaseRequest(lock_id=lock_id), _request(), other_creator, db_session)
    assert response["data"] == {"status":"not_found", "released":False}
    assert db_session.get(EditLock, lock_id).released_at is None


def test_own_edit_lock_lifecycle_is_preserved(target, regression_actor, db_session):
    from app.api.routes.edit_locks import release_edit_lock
    acquired = acquire_edit_lock(target, _request(), regression_actor, db_session)
    lock_id = UUID(acquired["data"]["lock"]["lock_id"])
    payload = EditLockReleaseRequest(lock_id=lock_id)
    assert heartbeat_edit_lock(payload, _request(), regression_actor, db_session)["data"]["status"] == "locked_by_me"
    assert release_edit_lock(payload, _request(), regression_actor, db_session)["data"]["released"] is True
