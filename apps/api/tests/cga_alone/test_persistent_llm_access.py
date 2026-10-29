"""공유 키 legacy 경로가 보호 방을 우회하지 못하는 실제PG 회귀."""

from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker
from app.models import Bot, BotVersion, ChannelRoom, ChannelQueueEvent, User
from app.api.routes import channels, bots
from app.core.config import settings
from app.services.version_pins import open_pinned_dialog
from tests.integration.test_bot_persistence_regression import _request


@pytest.fixture
def protected(persistent_context, monkeypatch):
    c = persistent_context
    monkeypatch.setattr(channels, "SessionLocal", sessionmaker(bind=c.engine))
    monkeypatch.setattr(settings, "webchat_api_key", "synthetic-shared-key")
    with Session(c.engine) as db:
        bot = db.get(Bot, c.bot_id)
        version = db.get(BotVersion, c.version_id)
        bot.active_version_id = version.id
        version.status = "active"
        room = db.get(ChannelRoom, c.room_id)
        room.channel_type = "webchat"
        client_room_id = room.client_room_id
        open_pinned_dialog(db, c.scope, {"instructions": "고정 지침"})
        db.commit()
    return c, client_room_id


@pytest.mark.parametrize("action", ["read", "write", "close", "reuse"])
def test_shared_key_cannot_read_write_close_or_rebind_protected_room(protected, action):
    c, client_id = protected
    request = _request()
    key = "synthetic-shared-key"
    with pytest.raises(HTTPException) as error:
        if action == "read":
            channels.get_channel_room("webchat", c.room_id, request, key)
        elif action == "write":
            channels.create_channel_room_message(
                "webchat",
                c.room_id,
                channels.ChannelMessageRequest(message="질문"),
                request,
                key,
            )
        elif action == "close":
            channels.delete_channel_room("webchat", c.room_id, request, key)
        else:
            channels.create_channel_room(
                "webchat",
                channels.ChannelRoomCreateRequest(
                    bot_id=str(c.bot_id),
                    client_room_id=client_id,
                    start_immediately=False,
                ),
                request,
                key,
            )
    assert error.value.status_code in {403, 404}
    with Session(c.engine) as db:
        room = db.get(ChannelRoom, c.room_id)
        assert (
            room.status == "open"
            and room.bot_version_id == c.version_id
            and room.client_room_id == client_id
        )


def test_client_participant_is_not_identity(protected):
    c, _ = protected
    result = channels.list_channel_rooms(
        "webchat", _request(), str(c.user_id), "synthetic-shared-key"
    )["data"]
    assert all(item["id"] != str(c.room_id) for item in result["rooms"])


def test_disabling_feature_does_not_remove_room_acl(protected):
    c, _ = protected
    assert getattr(settings, "persistent_llm_enabled", False) is False
    with pytest.raises(HTTPException) as error:
        channels.get_channel_room(
            "webchat", c.room_id, _request(), "synthetic-shared-key"
        )
    assert error.value.status_code in {403, 404}


def test_legacy_worker_never_executes_persistent_event(protected, monkeypatch):
    c, _ = protected
    executed = []
    monkeypatch.setattr(
        channels,
        "_process_channel_queue_event",
        lambda db, request, channel, event: executed.append(event.id)
        or {"id": str(event.id)},
    )
    with Session(c.engine) as db:
        event = ChannelQueueEvent(
            room_id=c.room_id,
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            channel_type="webchat",
            operation_kind="persistent_llm",
            status="queued",
        )
        db.add(event)
        db.commit()
        assert (
            channels._process_queued_channel_events(db, _request()) == []
            and executed == []
        )
        assert db.get(ChannelQueueEvent, event.id).status == "queued"


def test_active_version_change_preserves_pinned_sample_only(protected):
    c, _ = protected
    with Session(c.engine) as db:
        ordinary = ChannelRoom(
            channel_type="webchat",
            client_room_id=str(uuid4()),
            bot_id=c.bot_id,
            bot_version_id=c.version_id,
            participant_id="ordinary",
        )
        db.add(ordinary)
        db.commit()
        count = bots._close_open_channel_rooms_for_version_change(
            db,
            _request(),
            db.get(User, c.user_id),
            db.get(Bot, c.bot_id),
            c.version_id,
            uuid4(),
        )
        db.commit()
        assert count == 1
        assert (
            db.get(ChannelRoom, c.room_id).status == "open"
            and db.get(ChannelRoom, ordinary.id).status == "closed"
        )


def test_persistent_execution_defaults_off_without_implicit_budget():
    from app.core.config import Settings

    isolated = Settings(_env_file=None)
    assert isolated.persistent_llm_enabled is False
    assert isolated.persistent_llm_execution_enabled is False
    assert isolated.persistent_llm_sample_bot_id == ""
    assert isolated.persistent_llm_budget_scope == ""
    assert isolated.persistent_llm_generation_limit == 0
    assert isolated.persistent_llm_budget_amount == 0
    assert isolated.persistent_llm_context_input_tokens == 0
    assert isolated.persistent_llm_max_output_tokens == 0
    assert isolated.persistent_llm_input_rate_units_per_million is None
    assert isolated.persistent_llm_output_rate_units_per_million is None
    assert isolated.persistent_llm_tariff_model == ""


@pytest.fixture
def authenticated_http(persistent_context, monkeypatch):
    from copy import deepcopy
    from datetime import date
    from fastapi.testclient import TestClient
    from sqlalchemy import delete
    from app.main import app
    from app.api.deps import get_db
    from app.core.security import create_access_token
    from app.models import AdminLicense
    from app.services.llm_client import LlmChatClient
    from tests.cga_alone.test_persistent_llm_recovery import MemoryTransport
    from tests.cga_alone.test_persistent_llm_provider import CONFIG

    c = persistent_context
    import importlib

    qa_sessions = sessionmaker(bind=c.engine, autoflush=False)
    monkeypatch.setattr(channels, "SessionLocal", qa_sessions)
    monkeypatch.setattr(
        importlib.import_module("app.main"), "SessionLocal", qa_sessions
    )
    monkeypatch.setattr(
        importlib.import_module("app.api.deps"), "SessionLocal", qa_sessions
    )

    class ScopedTransport(MemoryTransport):
        def __init__(self):
            super().__init__()
            self.conversations = {}

        def __call__(self, method, url, payload, headers, timeout):
            if method == "POST" and url.endswith("/conversations"):
                self.calls.append((method, url, deepcopy(payload)))
                cid = "conv_" + str(len(self.conversations) + 1)
                self.conversations[cid] = []
                return {"id": cid}
            if "/conversations/" in url:
                cid = url.split("/conversations/", 1)[1].split("/", 1)[0]
                self.items = self.conversations[cid]
            return super().__call__(method, url, payload, headers, timeout)

    transport = ScopedTransport()
    for name, value in {
        "persistent_llm_enabled": True,
        "persistent_llm_execution_enabled": True,
        "persistent_llm_sample_bot_id": str(c.bot_id),
        "persistent_llm_budget_scope": "pllm-" + str(c.org_id),
        "persistent_llm_tariff_model": CONFIG.model,
        "persistent_llm_tariff_version": "test-v1",
        "persistent_llm_currency": "USD",
        "persistent_llm_context_input_tokens": 100000,
        "persistent_llm_max_output_tokens": 64,
        "persistent_llm_budget_amount": 10000,
        "persistent_llm_generation_limit": 10,
        "persistent_llm_input_rate_units_per_million": 1000,
        "persistent_llm_output_rate_units_per_million": 2000,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("AIDOT_CHATGPT_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("AIDOT_CHATGPT_BASE_URL", CONFIG.base_url)
    monkeypatch.setattr(
        bots, "_persistent_provider_transport", lambda: transport, raising=False
    )

    def forbidden_legacy(*args, **kwargs):
        raise AssertionError("legacy single-shot/NLU generation must remain separate")

    monkeypatch.setattr(LlmChatClient, "chat", forbidden_legacy)
    with Session(c.engine) as db:
        bot = db.get(Bot, c.bot_id)
        version = db.get(BotVersion, c.version_id)
        bot.active_version_id = version.id
        bot.data_json = {
            "nlu_type": "ml",
            "nlu_model": "deep_learning_lite",
            "answer_mode": "fixed",
            "llm_provider": CONFIG.provider,
            "llm_model": CONFIG.model,
        }
        doc = deepcopy(version.version_json)
        doc["dialogs"] = [
            {
                "id": "pllm-module",
                "name": "지속대화 샘플",
                "dialogType": 0,
                "llmAnswerPrompt": "고정 지침",
            }
        ]
        doc["dialog_flow_graphs"] = [
            {
                "id": "pllm-graph",
                "dialogId": "pllm-module",
                "name": "지속대화 샘플",
                "nodes": [
                    {"id": "start", "kind": "start", "config": {}},
                    {
                        "id": "talk",
                        "kind": "talk",
                        "config": {
                            "messageType": "text",
                            "basicMessages": ["답변: {{$_llm_answer_text}}"],
                            "responseType": "none",
                        },
                    },
                    {
                        "id": "end",
                        "kind": "end",
                        "config": {"endSessionImmediately": True},
                    },
                ],
                "links": [
                    {
                        "sourceNodeId": "start",
                        "sourcePort": "next",
                        "targetNodeId": "talk",
                    },
                    {
                        "sourceNodeId": "talk",
                        "sourcePort": "next",
                        "targetNodeId": "end",
                    },
                ],
            }
        ]
        bots._assign_version_document(version, doc)
        version.status = "active"
        other = User(
            organization_id=c.org_id,
            group_id=c.group_id,
            login_id="pllm-other-" + uuid4().hex[:15],
            password_hash="test-only",
            name="다른 제작자",
            status="active",
        )
        license = AdminLicense(
            organization_id=c.org_id,
            license_id="pllm-license",
            product="CGA",
            customer_name="합성검증",
            license_text="test-only",
            signature_value="test-only",
            payload_json={"expires_at": "2099-12-31"},
        )
        db.add_all([other, license])
        db.commit()
        other_id = other.id
        license_id = license.id

    def database():
        with qa_sessions() as db:
            yield db

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    client = TestClient(app)
    a = {"Authorization": "Bearer " + create_access_token(str(c.user_id))}
    b = {"Authorization": "Bearer " + create_access_token(str(other_id))}
    try:
        yield c, client, a, b, transport, license_id
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
        with Session(c.engine) as db:
            db.execute(delete(AdminLicense).where(AdminLicense.id == license_id))
            db.execute(delete(User).where(User.id == other_id))
            db.commit()


def http_open(fixture):
    c, client, a, _, _, _ = fixture
    base = f"/api/v1/bots/{c.bot_id}/persistent"
    opened = client.post(base + "/rooms", headers=a, json={"dialog_id": "pllm-module"})
    assert opened.status_code in {200, 201}, opened.text
    return base, opened.json()["data"]["room"]["id"]


def http_process(
    fixture, base, room_id, key="message-1", question="첫 질문", dialog_id="pllm-module"
):
    c, client, a, _, _, _ = fixture
    sent = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": key,
            "question": question,
            "dialog_id": dialog_id,
        },
    )
    assert sent.status_code in {200, 202}, sent.text
    event_id = sent.json()["data"]["event_id"]
    with Session(c.engine) as db:
        bots._process_persistent_llm_events(db, _request(), limit=10)
    return (
        event_id,
        client.get(base + f"/rooms/{room_id}/turns/{event_id}", headers=a).json()[
            "data"
        ],
    )


def http_confirm(fixture, base, room_id, event_id, view):
    c, client, a, _, _, _ = fixture
    ack = client.post(
        base + f"/rooms/{room_id}/turns/{event_id}/ack",
        headers=a,
        json={
            "message_id": view["message"]["id"],
            "content_hash": view["message"]["hash"],
        },
    )
    assert ack.status_code in {200, 202}, ack.text
    with Session(c.engine) as db:
        bots._process_persistent_llm_events(db, _request(), limit=10)
    return client.get(base + f"/rooms/{room_id}/turns/{event_id}", headers=a).json()[
        "data"
    ]


def test_authenticated_http_uses_server_talk_and_end_keeps_binding(authenticated_http):
    from app.models import ChannelLlmBinding

    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    assert (
        view["phase"] == "pending_delivery"
        and view["message"]["text"] == "답변: 보여줄 답변"
    )
    assert (
        http_confirm(authenticated_http, base, room_id, event, view)["phase"]
        == "committed"
    )
    with Session(c.engine) as db:
        room = db.get(ChannelRoom, __import__("uuid").UUID(room_id))
        binding = db.scalar(
            __import__("sqlalchemy")
            .select(ChannelLlmBinding)
            .where(ChannelLlmBinding.room_id == room.id)
        )
        assert (
            room.status == "open"
            and binding.status == "open"
            and binding.provider_conversation_id
        )
        assert room.metadata_json.get("dialogEnded") is True
    assert transport.generations == 1


def test_authenticated_b_cannot_use_a_room_or_forge_participant(authenticated_http):
    c, client, a, b, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    assert client.get(base + f"/rooms/{room_id}", headers=b).status_code == 404
    assert (
        client.post(
            base + f"/rooms/{room_id}/turns",
            headers=b,
            json={
                "expected_generation": 1,
                "message_key": "b",
                "question": "위장",
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            base + f"/rooms/{room_id}/turns",
            headers=a,
            json={
                "expected_generation": 1,
                "message_key": "fake",
                "question": "위장",
                "participant_id": "other",
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 422
    )
    assert (
        client.get(
            base + f"/rooms/{room_id}", headers={"Authorization": "Bearer forged"}
        ).status_code
        == 401
    )
    assert transport.generations == 0


def test_two_rooms_of_same_creator_keep_separate_transcripts(authenticated_http):
    c, client, a, _, transport, _ = authenticated_http
    base, room1 = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room1, question="방1 비밀")
    http_confirm(authenticated_http, base, room1, event, view)
    _, room2 = http_open(authenticated_http)
    assert room1 != room2
    http_process(authenticated_http, base, room2, "room2-message", "방2 질문")
    payload = [
        call[2]
        for call in transport.calls
        if call[0] == "POST" and call[1].endswith("/responses")
    ][-1]
    assert len(payload["input"]) == 1 and "방1 비밀" not in __import__("json").dumps(
        payload, ensure_ascii=False
    )


def test_feature_off_still_blocks_legacy_and_new_paid_execution(
    authenticated_http, monkeypatch
):
    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    monkeypatch.setattr(settings, "persistent_llm_enabled", False)
    sent = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": "disabled",
            "question": "질문",
            "dialog_id": "pllm-module",
        },
    )
    assert sent.status_code in {409, 503}
    with pytest.raises(HTTPException):
        channels.get_channel_room(
            "webchat", __import__("uuid").UUID(room_id), _request(), None
        )
    assert transport.generations == 0


def test_license_revocation_blocks_persistent_output_and_generation(authenticated_http):
    from app.models import AdminLicense

    c, client, a, _, transport, license_id = authenticated_http
    base, room_id = http_open(authenticated_http)
    with Session(c.engine) as db:
        db.get(AdminLicense, license_id).status = "revoked"
        db.commit()
    assert client.get(base + f"/rooms/{room_id}", headers=a).status_code == 403
    assert (
        client.post(
            base + f"/rooms/{room_id}/turns",
            headers=a,
            json={
                "expected_generation": 1,
                "message_key": "revoked",
                "question": "질문",
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 403
    )
    assert transport.generations == 0


@pytest.mark.parametrize(
    "problem", ["extra_start", "bad_link", "literal_variable_name"]
)
def test_sample_profile_does_not_flatten_unsupported_or_charge_literal_text(problem):
    nodes = [
        {"id": "start", "kind": "start", "config": {}},
        {
            "id": "talk",
            "kind": "talk",
            "config": {"basicMessages": ["고정값"], "messageType": "text"},
        },
        {"id": "end", "kind": "end", "config": {}},
    ]
    links = [
        {"sourceNodeId": "start", "sourcePort": "next", "targetNodeId": "talk"},
        {"sourceNodeId": "talk", "sourcePort": "next", "targetNodeId": "end"},
    ]
    if problem == "extra_start":
        nodes.append({"id": "orphan-start", "kind": "start", "config": {}})
    elif problem == "bad_link":
        links.append("malformed")
    else:
        nodes[1]["config"]["basicMessages"] = ["문자 그대로 _llm_answer_text"]
    document = {
        "dialogs": [{"id": "d", "name": "검증"}],
        "dialog_flow_graphs": [
            {"id": "g", "dialogId": "d", "nodes": nodes, "links": links}
        ],
    }
    profiles = bots._persistent_profiles(document)
    if problem == "literal_variable_name":
        assert profiles["d"]["uses_llm"] is False
    else:
        assert profiles == {}


def test_configured_sample_copy_registers_guard_before_first_exposure(
    persistent_context, monkeypatch
):
    from app.models import ChannelLlmVersionGuard
    from sqlalchemy import delete
    from uuid import UUID

    c = persistent_context
    monkeypatch.setattr(settings, "persistent_llm_sample_bot_id", str(c.bot_id))
    with Session(c.engine) as db:
        db.execute(
            delete(ChannelLlmVersionGuard).where(
                ChannelLlmVersionGuard.version_id == c.version_id
            )
        )
        db.commit()
        result = bots.copy_version(
            c.bot_id, c.version_id, _request(), db.get(User, c.user_id), db
        )["data"]
        guard = db.get(ChannelLlmVersionGuard, UUID(result["id"]))
        assert guard is not None and guard.mutation_status == "idle"


def test_existing_main_worker_routes_persistent_events_without_legacy_execution(
    authenticated_http,
):
    from types import SimpleNamespace
    from app import main

    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    submitted = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": "main-worker",
            "question": "질문",
            "dialog_id": "pllm-module",
        },
    )
    assert submitted.status_code == 202
    assert main._process_queue_worker_batch(SimpleNamespace(client=None), 10) == 1
    assert transport.generations == 1


def test_fixed_qa_is_canonical_without_generation_and_next_llm_sees_it(
    authenticated_http,
):
    from copy import deepcopy

    c, client, a, _, transport, _ = authenticated_http
    with Session(c.engine) as db:
        version = db.get(BotVersion, c.version_id)
        doc = deepcopy(version.version_json)
        doc["dialogs"].append(
            {"id": "fixed-module", "name": "고정답변", "dialogType": 0}
        )
        graph = deepcopy(doc["dialog_flow_graphs"][0])
        graph.update(id="fixed-graph", dialogId="fixed-module")
        graph["nodes"][1]["config"]["basicMessages"] = ["사전에 정의한 답변"]
        doc["dialog_flow_graphs"].append(graph)
        bots._assign_version_document(version, doc)
        db.commit()
    base, room_id = http_open(authenticated_http)
    event, view = http_process(
        authenticated_http,
        base,
        room_id,
        "fixed-question",
        "고정 질문",
        dialog_id="fixed-module",
    )
    assert (
        view["phase"] == "pending_delivery"
        and view["message"]["text"] == "사전에 정의한 답변"
    )
    assert transport.generations == 0
    assert (
        http_confirm(authenticated_http, base, room_id, event, view)["phase"]
        == "committed"
    )
    current = client.get(base + f"/rooms/{room_id}", headers=a).json()["data"]["room"]
    assert current["scenario_ended"] is True
    http_process(authenticated_http, base, room_id, "llm-followup", "이어서 질문")
    assert transport.generations == 1
    payload = [
        row[2]
        for row in transport.calls
        if row[0] == "POST" and row[1].endswith("/responses")
    ][-1]
    assert (
        payload["input"][0]["content"] == "고정 질문"
        and payload["input"][1]["content"] == "사전에 정의한 답변"
    )
