from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes.bots import create_bot, copy_version
from app.models import Bot, BotVersion
from app.schemas.bot import BotCreateRequest
from tests.integration.test_bot_persistence_regression import _request


def test_second_bot_creation_is_rejected(regression_actor, db_session):
    create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    with pytest.raises(HTTPException) as denied:
        create_bot(BotCreateRequest(name="두 번째"), _request(), regression_actor, db_session)
    assert denied.value.status_code == 409
    assert len(db_session.scalars(select(Bot).where(Bot.created_by == regression_actor.id, Bot.deleted_at.is_(None))).all()) == 1


def test_initial_bot_name_is_display_and_call_name(regression_actor, db_session):
    response = create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    bot = db_session.get(Bot, UUID(response["data"]["id"]))
    assert response["data"]["name"] == bot.name == "다온"
    assert "call_name" not in BotCreateRequest.model_fields


def test_copy_version_is_allowed(regression_actor, db_session):
    response = create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    bot_id = UUID(response["data"]["id"])
    version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == bot_id))
    copied = copy_version(bot_id, version.id, _request(), regression_actor, db_session)
    assert copied["data"]["id"] != str(version.id)
    assert len(db_session.scalars(select(Bot).where(Bot.created_by == regression_actor.id)).all()) == 1


def test_database_rejects_second_owned_bot(regression_actor, db_session):
    from sqlalchemy.exc import IntegrityError
    create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(Bot(organization_id=regression_actor.organization_id,
                               group_id=regression_actor.group_id, created_by=regression_actor.id,
                               name="직접 삽입 우회", data_json={}))
            db_session.flush()


def test_parallel_creation_has_one_winner(test_engine, migrated_test_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import delete, update
    from sqlalchemy.orm import Session
    from app.models import AdminLicense, AuditLog, Group, Organization, User
    from tests.integration.test_bot_persistence_regression import regression_actor as actor_fixture
    with Session(test_engine, expire_on_commit=False) as session:
        actor = actor_fixture.__wrapped__(session, migrated_test_database)
        actor_id, org_id, group_id = actor.id, actor.organization_id, actor.group_id
    barrier = Barrier(2)
    def create_one(number):
        with Session(test_engine, expire_on_commit=False) as session:
            user = session.get(User, actor_id)
            barrier.wait(timeout=15)
            try:
                create_bot(BotCreateRequest(name=f"동시 요청 {number}"), _request(), user, session)
                return 200
            except HTTPException as error:
                session.rollback()
                return error.status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_one, range(2)))
        with Session(test_engine) as session:
            bots = session.scalars(select(Bot).where(Bot.created_by == actor_id, Bot.deleted_at.is_(None))).all()
            assert len(bots) == 1
            assert sorted(results) == [200, 409]
    finally:
        with Session(test_engine) as session:
            ids = session.scalars(select(Bot.id).where(Bot.created_by == actor_id)).all()
            session.execute(delete(AuditLog).where(AuditLog.actor_user_id == actor_id))
            session.execute(update(Bot).where(Bot.id.in_(ids)).values(active_version_id=None))
            session.execute(delete(BotVersion).where(BotVersion.bot_id.in_(ids)))
            session.execute(delete(Bot).where(Bot.id.in_(ids)))
            session.execute(delete(AdminLicense).where(AdminLicense.organization_id == org_id))
            session.execute(delete(User).where(User.id == actor_id))
            session.execute(delete(Group).where(Group.id == group_id))
            session.execute(delete(Organization).where(Organization.id == org_id))
            session.commit()


def test_import_cannot_create_second_bot(regression_actor, db_session):
    from app.api.routes.bots import export_aidot_bot_package, import_aidot_bot_package
    first = create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    bot_id = UUID(first["data"]["id"])
    version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == bot_id))
    package = export_aidot_bot_package(bot_id, version.id, _request(), regression_actor, db_session)["data"]
    import_aidot_bot_package(bot_id, version.id, package, _request(), regression_actor, db_session)
    assert len(db_session.scalars(select(Bot).where(Bot.created_by == regression_actor.id, Bot.deleted_at.is_(None))).all()) == 1
    with pytest.raises(HTTPException) as denied:
        create_bot(BotCreateRequest(name="가져오기 이후 새 봇"), _request(), regression_actor, db_session)
    assert denied.value.status_code == 409


def test_soft_deleted_bot_can_be_recreated(regression_actor, db_session):
    from app.api.routes.bots import delete_bot
    first = create_bot(BotCreateRequest(name="삭제 전"), _request(), regression_actor, db_session)
    delete_bot(UUID(first["data"]["id"]), _request(), regression_actor, db_session)
    second = create_bot(BotCreateRequest(name="삭제 후"), _request(), regression_actor, db_session)
    assert first["data"]["id"] != second["data"]["id"]
    assert len(db_session.scalars(select(Bot).where(Bot.created_by == regression_actor.id, Bot.deleted_at.is_(None))).all()) == 1


def test_unowned_system_bots_do_not_consume_creator_slot(regression_actor, db_session):
    for name in ["system-one", "system-two"]:
        db_session.add(Bot(organization_id=regression_actor.organization_id,
                           group_id=regression_actor.group_id, created_by=None, name=name, data_json={}))
    db_session.flush()
    owned = create_bot(BotCreateRequest(name="다온"), _request(), regression_actor, db_session)
    assert db_session.get(Bot, UUID(owned["data"]["id"])).created_by == regression_actor.id
