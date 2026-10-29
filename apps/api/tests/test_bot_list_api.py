from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.params import Query

from app.api.routes import bots
from app.api.routes.bots import (
    _api_catalog_usage_counts,
    _serialize_bot_api_catalog_summary,
    get_bot_settings_context,
    list_bots,
    list_versions,
    router,
    update_bot,
)


def test_list_bots_defaults_to_summary_payload() -> None:
    include_document = inspect.signature(list_bots).parameters["include_document"].default

    assert isinstance(include_document, Query)
    assert include_document.default is False


def test_list_versions_defaults_to_summary_payload() -> None:
    include_document = inspect.signature(list_versions).parameters["include_document"].default

    assert isinstance(include_document, Query)
    assert include_document.default is False


def test_list_versions_summary_cache_miss_does_not_reload_full_documents(monkeypatch) -> None:
    bot_id = uuid4()
    version_id = uuid4()
    bot = SimpleNamespace(
        id=bot_id,
        active_version_id=version_id,
        updated_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )

    class SnapshotVersion:
        def __init__(self) -> None:
            self.id = version_id
            self.bot_id = bot_id
            self.version_no = 1
            self.name = "v1"
            self.description = None
            self.status = "draft"
            self.comment = None
            self.copied_from_version_id = None
            self.activated_at = None
            self.updated_at = datetime(2026, 5, 18, tzinfo=timezone.utc)
            self.created_at = datetime(2026, 5, 18, tzinfo=timezone.utc)
            self.asset_counts_json = {"dialogs": 2, "intents": 1, "modules": 1}
            self.scenario_validation_json = {"error_count": 0}
            self.nlu_training_json = {}

        @property
        def version_json(self) -> dict[str, object]:
            raise AssertionError("summary list should not reload version_json")

    calls: list[bool] = []

    def _fake_list_versions(_db: object, _bot_id: object, *, include_document: bool) -> list[SnapshotVersion]:
        calls.append(include_document)
        return [SnapshotVersion()]

    monkeypatch.setattr(bots, "_get_bot_or_404", lambda _db, _bot_id, _user: bot)
    monkeypatch.setattr(bots, "_list_bot_versions", _fake_list_versions)
    monkeypatch.setattr(bots, "cache_aside_json", lambda _key, producer, **_kwargs: producer())

    response = bots.list_versions(
        bot_id=bot_id,
        request=SimpleNamespace(state=SimpleNamespace(request_id="test")),
        include_document=False,
        current_user=SimpleNamespace(),
        db=None,
    )

    assert calls == [False]
    assert response["data"][0]["asset_counts"]["dialogs"] == 2


def test_settings_context_cache_miss_does_not_reload_all_version_documents(monkeypatch) -> None:
    user_id = uuid4()
    bot_id = uuid4()
    version_id = uuid4()
    group_id = uuid4()
    organization_id = uuid4()
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    bot = SimpleNamespace(
        id=bot_id,
        organization_id=organization_id,
        group_id=group_id,
        active_version_id=version_id,
        name="테스트봇",
        description=None,
        status="active",
        data_json={},
        created_by=user_id,
        updated_at=now,
        created_at=now,
    )

    class SettingsVersion:
        def __init__(self) -> None:
            self.id = version_id
            self.bot_id = bot_id
            self.version_no = 1
            self.name = "v1"
            self.description = None
            self.status = "draft"
            self.comment = None
            self.copied_from_version_id = None
            self.activated_at = None
            self.updated_at = now
            self.created_at = now
            self.asset_counts_json = {}
            self.scenario_validation_json = {}
            self.nlu_training_json = {"status": "success"}

        @property
        def version_json(self) -> dict[str, object]:
            return {"system_config": {"ai_config": {"nlu_type": "semantic"}}}

    class SettingsDb:
        def scalar(self, _statement: object) -> object:
            return bot if not hasattr(self, "_returned_bot") else None

    calls: list[bool] = []

    def _fake_list_versions(_db: object, _bot_id: object, *, include_document: bool) -> list[SettingsVersion]:
        calls.append(include_document)
        return [SettingsVersion()]

    db = SettingsDb()
    db._returned_bot = False  # type: ignore[attr-defined]

    def _scalar_once(_statement: object) -> object:
        if db._returned_bot is False:  # type: ignore[attr-defined]
            db._returned_bot = True  # type: ignore[attr-defined]
            return bot
        return None

    db.scalar = _scalar_once  # type: ignore[method-assign]
    monkeypatch.setattr(bots, "_list_bot_versions", _fake_list_versions)
    monkeypatch.setattr(bots, "cache_aside_json", lambda _key, producer, **_kwargs: producer())

    response = get_bot_settings_context(
        bot_id=str(bot_id),
        version_scope="v1",
        request=SimpleNamespace(state=SimpleNamespace(request_id="test")),
        current_user=SimpleNamespace(id=user_id, group_id=group_id, organization_id=organization_id),
        db=db,  # type: ignore[arg-type]
    )

    assert calls == [False]
    assert response["data"]["version"]["version_json"]["system_config"]["ai_config"]["nlu_type"] == "semantic"


def test_update_bot_defaults_to_full_response_payload() -> None:
    response_mode = inspect.signature(update_bot).parameters["response_mode"].default

    assert isinstance(response_mode, Query)
    assert response_mode.default == "full"


def test_api_catalog_route_is_registered_before_dynamic_bot_route() -> None:
    paths = [route.path for route in router.routes]

    assert paths.index("/bots/api-catalog") < paths.index("/bots/{bot_id}")


def test_settings_context_route_is_registered_before_dynamic_bot_route() -> None:
    paths = [route.path for route in router.routes]

    assert "/bots/{bot_id}/versions/{version_scope}/settings-context" in paths
    assert paths.index("/bots/{bot_id}/versions/{version_scope}/settings-context") < paths.index("/bots/{bot_id}")


def test_api_catalog_usage_counts_scans_nested_flow_nodes() -> None:
    apis = [{"id": "api-1"}, {"id": "api-2"}, {"id": "api-unused"}]
    graphs = [
        {"nodes": [{"data": {"apiId": "api-1"}}]},
        {"nodes": [{"data": {"actions": [{"apiId": "api-1"}, {"apiId": "api-2"}]}}]},
        {"nodes": [{"data": {"apiId": "api-missing"}}]},
    ]

    assert _api_catalog_usage_counts(apis, graphs) == {
        "api-1": 2,
        "api-2": 1,
        "api-unused": 0,
    }


def test_api_catalog_summary_uses_snapshots_without_full_document() -> None:
    bot_id = uuid4()
    version_id = uuid4()
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    bot = SimpleNamespace(
        id=bot_id,
        organization_id=uuid4(),
        group_id=uuid4(),
        active_version_id=version_id,
        name="테스트봇",
        description=None,
        status="active",
        data_json={},
        created_by=None,
        updated_at=now,
        created_at=now,
    )

    class SnapshotVersion:
        def __init__(self) -> None:
            self.id = version_id
            self.bot_id = bot_id
            self.version_no = 1
            self.name = "v1"
            self.description = None
            self.status = "active"
            self.comment = None
            self.copied_from_version_id = None
            self.activated_at = None
            self.updated_at = now
            self.created_at = now
            self.asset_counts_json = {"apis": 2}
            self.scenario_validation_json = {"error_count": 0, "items": []}
            self.nlu_training_json = {}
            self.apis_json = [{"id": "api-1"}, {"id": "api-2"}]

        @property
        def version_json(self) -> dict[str, object]:
            raise AssertionError("api catalog should not load version_json when snapshots are available")

    class ApiCatalogDb:
        def scalar(self, _statement: object) -> object:
            return None

        def scalars(self, _statement: object) -> object:
            return SimpleNamespace(
                all=lambda: [
                    SimpleNamespace(payload_json={"dialogId": "intent-1", "nodes": [{"data": {"apiId": "api-1"}}]}),
                    SimpleNamespace(payload_json={"dialogId": "intent-2", "nodes": [{"data": {"apiId": "api-1"}}]}),
                ]
            )

    payload = _serialize_bot_api_catalog_summary(
        ApiCatalogDb(),  # type: ignore[arg-type]
        bot,
        active_version_override=SnapshotVersion(),  # type: ignore[arg-type]
        version_count_override=1,
        audit_context={"training": {}},
    )

    apis = payload["active_version"]["version_json"]["apis"]
    assert apis == [{"id": "api-1", "usageCount": 2}, {"id": "api-2", "usageCount": 0}]


def test_api_catalog_summary_dedupes_duplicate_api_snapshots() -> None:
    bot_id = uuid4()
    version_id = uuid4()
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    bot = SimpleNamespace(
        id=bot_id,
        organization_id=uuid4(),
        group_id=uuid4(),
        active_version_id=version_id,
        name="테스트봇",
        description=None,
        status="active",
        data_json={},
        created_by=None,
        updated_at=now,
        created_at=now,
    )

    class SnapshotVersion:
        def __init__(self) -> None:
            self.id = version_id
            self.bot_id = bot_id
            self.version_no = 1
            self.name = "v1"
            self.description = None
            self.status = "active"
            self.comment = None
            self.copied_from_version_id = None
            self.activated_at = None
            self.updated_at = now
            self.created_at = now
            self.asset_counts_json = {"apis": 3}
            self.scenario_validation_json = {"error_count": 0, "items": []}
            self.nlu_training_json = {}
            self.apis_json = [{"id": "api-1"}, {"id": "api-1"}, {"id": "api-2"}]

        @property
        def version_json(self) -> dict[str, object]:
            raise AssertionError("api catalog should not load version_json when snapshots are available")

    class ApiCatalogDb:
        def scalar(self, _statement: object) -> object:
            return None

        def scalars(self, _statement: object) -> object:
            return SimpleNamespace(
                all=lambda: [
                    SimpleNamespace(payload_json={"dialogId": "intent-1", "nodes": [{"data": {"apiId": "api-1"}}]}),
                ]
            )

    payload = _serialize_bot_api_catalog_summary(
        ApiCatalogDb(),  # type: ignore[arg-type]
        bot,
        active_version_override=SnapshotVersion(),  # type: ignore[arg-type]
        version_count_override=1,
        audit_context={"training": {}},
    )

    assert payload["active_version"]["version_json"]["apis"] == [
        {"id": "api-1", "usageCount": 1},
        {"id": "api-2", "usageCount": 0},
    ]
