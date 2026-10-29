from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.routes.bots import _get_bot_or_404, create_bot, list_bots
from app.models import User
from app.schemas.bot import BotCreateRequest
from tests.integration.test_bot_persistence_regression import _request


@pytest.fixture
def other_creator(regression_actor, db_session):
    suffix = uuid4().hex[:12]
    user = User(organization_id=regression_actor.organization_id, group_id=regression_actor.group_id,
                login_id=f"alone-{suffix}", password_hash="test-only", name="제작자 B", status="active")
    db_session.add(user)
    db_session.flush()
    return user


def test_other_creator_gets_404(regression_actor, other_creator, db_session):
    created = create_bot(BotCreateRequest(name="제작자 A 봇"), _request(), regression_actor, db_session)
    with pytest.raises(HTTPException) as denied:
        _get_bot_or_404(db_session, UUID(created["data"]["id"]), other_creator)
    assert denied.value.status_code == 404


def test_list_only_contains_own_bot(regression_actor, other_creator, db_session):
    own = create_bot(BotCreateRequest(name="제작자 A 봇"), _request(), regression_actor, db_session)
    create_bot(BotCreateRequest(name="제작자 B 봇"), _request(), other_creator, db_session)
    result = list_bots(_request(), q=None, status_filter=None, include_document=False,
                      page=1, page_size=20, current_user=regression_actor, db=db_session)
    assert [item["id"] for item in result["data"]] == [own["data"]["id"]]
    assert result["meta"]["total"] == 1


@pytest.mark.parametrize("suffix", ["", "/versions", "/versions/{version_id}",
    "/versions/{version_id}/dialogs", "/versions/{version_id}/entities",
    "/versions/{version_id}/dictionary", "/versions/{version_id}/apis",
    "/versions/{version_id}/references", "/versions/{version_id}/configure",
    "/versions/{version_id}/retraining", "/versions/{version_id}/export",
    "/versions/{version_id}/aidot-package"])
def test_other_creator_resource_gets_404(suffix, regression_actor, other_creator, db_session):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api.deps import get_current_user, get_db
    from app.main import app
    from app.models import BotVersion
    created = create_bot(BotCreateRequest(name="격리 확인"), _request(), regression_actor, db_session)
    bot_id = UUID(created["data"]["id"])
    version = db_session.scalar(select(BotVersion).where(BotVersion.bot_id == bot_id))
    path = f"/api/v1/bots/{bot_id}" + suffix.format(version_id=version.id)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        client = TestClient(app)
        try:
            app.dependency_overrides[get_current_user] = lambda: regression_actor
            assert client.get(path).status_code == 200, path
            app.dependency_overrides[get_current_user] = lambda: other_creator
            assert client.get(path).status_code == 404, path
        finally:
            client.close()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_anonymous_questioner_cannot_edit_bot():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    try:
        assert client.patch(f"/api/v1/bots/{uuid4()}", json={"name":"침입"}).status_code == 401
    finally:
        client.close()


def test_bot_catalog_only_contains_own_bot(regression_actor, other_creator, db_session):
    from app.api.routes.bots import list_api_catalog_bots
    own = create_bot(BotCreateRequest(name="본인 봇"), _request(), regression_actor, db_session)
    create_bot(BotCreateRequest(name="다른 제작자"), _request(), other_creator, db_session)
    result = list_api_catalog_bots(_request(), regression_actor, db_session)
    assert [item["id"] for item in result["data"]] == [own["data"]["id"]]


def test_hub_owner_isolation_keeps_hub_api(regression_actor, other_creator, db_session):
    from app.api.routes.hubs import _get_hub_bot, list_hubs
    own = create_bot(BotCreateRequest(name="기존 허브", bot_kind="hub"), _request(), regression_actor, db_session)
    bot_id = UUID(own["data"]["id"])
    assert _get_hub_bot(db_session, regression_actor, bot_id)[0].id == bot_id
    with pytest.raises(HTTPException) as denied:
        _get_hub_bot(db_session, other_creator, bot_id)
    assert denied.value.status_code == 404
    assert list_hubs(_request(), other_creator, db_session)["data"] == []
