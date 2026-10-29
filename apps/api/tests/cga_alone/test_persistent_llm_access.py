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
            self.namespace = uuid4().hex

        def __call__(self, method, url, payload, headers, timeout):
            if method == "POST" and url.endswith("/conversations"):
                self.calls.append((method, url, deepcopy(payload)))
                cid = "conv_" + self.namespace + "_" + str(len(self.conversations) + 1)
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
    fixture,
    base,
    room_id,
    key="message-1",
    question="첫 질문",
    dialog_id="pllm-module",
    expected_generation=1,
):
    c, client, a, _, _, _ = fixture
    sent = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": expected_generation,
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
    assert (
        client.get(base + f"/rooms/{room_id}", headers=a).json()["data"]["room"][
            "can_send"
        ]
        is False
    )
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


def test_new_dialog_has_fresh_id_and_closed_generation_history(authenticated_http):
    from sqlalchemy import select
    from app.models import ChannelLlmBinding
    from uuid import UUID

    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    http_confirm(authenticated_http, base, room_id, event, view)
    with Session(c.engine) as db:
        old_cid = db.scalar(
            select(ChannelLlmBinding).where(ChannelLlmBinding.room_id == UUID(room_id))
        ).provider_conversation_id
    reset = client.post(
        base + f"/rooms/{room_id}/new",
        headers=a,
        json={
            "expected_generation": 1,
            "confirm_reset": True,
            "dialog_id": "pllm-module",
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["room"]["generation"] == 2
    old = client.get(base + f"/rooms/{room_id}?generation=1", headers=a).json()["data"]
    assert old["room"]["status"] == "closed" and len(old["messages"]) == 2
    assert (
        client.get(base + f"/rooms/{room_id}", headers=a).json()["data"]["messages"]
        == []
    )
    assert (
        client.post(
            base + f"/rooms/{room_id}/turns",
            headers=a,
            json={
                "expected_generation": 1,
                "message_key": "late-old",
                "question": "늦은 이전 질문",
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 409
    )
    http_process(
        authenticated_http,
        base,
        room_id,
        "new-message",
        "새 문맥 질문",
        expected_generation=2,
    )
    with Session(c.engine) as db:
        new_cid = db.scalar(
            select(ChannelLlmBinding).where(
                ChannelLlmBinding.room_id == UUID(room_id),
                ChannelLlmBinding.generation == 2,
            )
        ).provider_conversation_id
    assert new_cid and new_cid != old_cid
    body = [
        v[2] for v in transport.calls if v[0] == "POST" and v[1].endswith("/responses")
    ][-1]
    assert len(body["input"]) == 1
    assert (
        client.post(
            base + f"/rooms/{room_id}/new",
            headers=a,
            json={
                "expected_generation": 1,
                "confirm_reset": True,
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 409
    )


def test_explicit_close_keeps_history_read_only_without_replay(authenticated_http):
    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    http_confirm(authenticated_http, base, room_id, event, view)
    closed = client.post(
        base + f"/rooms/{room_id}/end", headers=a, json={"expected_generation": 1}
    )
    assert closed.status_code == 200, closed.text
    history = client.get(base + f"/rooms/{room_id}", headers=a)
    assert (
        history.status_code == 200
        and history.json()["data"]["room"]["status"] == "closed"
    )
    assert len(history.json()["data"]["messages"]) == 2
    assert (
        client.post(
            base + f"/rooms/{room_id}/turns",
            headers=a,
            json={
                "expected_generation": 1,
                "message_key": "closed",
                "question": "종료 후 질문",
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 409
    )
    _, next_room = http_open(authenticated_http)
    assert (
        next_room != room_id
        and client.get(base + f"/rooms/{next_room}", headers=a).json()["data"][
            "messages"
        ]
        == []
    )
    assert transport.generations == 1


def test_reset_and_close_cannot_unpin_pending_delivery(authenticated_http):
    from app.models import ChannelLlmBinding
    from sqlalchemy import select
    from uuid import UUID

    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    assert view["phase"] == "pending_delivery"
    assert (
        client.post(
            base + f"/rooms/{room_id}/new",
            headers=a,
            json={
                "expected_generation": 1,
                "confirm_reset": True,
                "dialog_id": "pllm-module",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + f"/rooms/{room_id}/end", headers=a, json={"expected_generation": 1}
        ).status_code
        == 409
    )
    with Session(c.engine) as db:
        binding = db.scalar(
            select(ChannelLlmBinding).where(ChannelLlmBinding.room_id == UUID(room_id))
        )
        assert binding.generation == 1 and binding.status == "open"
    assert transport.generations == 1


def test_known_completed_cancel_does_not_recall_provider_and_allows_reset(
    authenticated_http,
):
    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    before = len(transport.calls)
    canceled = client.post(
        base + f"/rooms/{room_id}/turns/{event}/cancel", headers=a, json={}
    )
    assert canceled.status_code == 200, canceled.text
    assert (
        canceled.json()["data"]["phase"] == "cancelled"
        and canceled.json()["data"]["message"] is None
    )
    assert len(transport.calls) == before and transport.generations == 1
    history = client.get(base + f"/rooms/{room_id}", headers=a).json()["data"][
        "messages"
    ]
    assert not any(row["sender"] == "bot" for row in history)
    reset = client.post(
        base + f"/rooms/{room_id}/new",
        headers=a,
        json={
            "expected_generation": 1,
            "confirm_reset": True,
            "dialog_id": "pllm-module",
        },
    )
    assert reset.status_code == 200 and reset.json()["data"]["room"]["generation"] == 2


def test_off_allows_proven_presubmit_cancel_without_refunding_unknown(
    authenticated_http, monkeypatch
):
    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    sent = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": "not-submitted",
            "question": "질문",
            "dialog_id": "pllm-module",
        },
    )
    assert sent.status_code == 202
    event = sent.json()["data"]["event_id"]
    monkeypatch.setattr(settings, "persistent_llm_enabled", False)
    canceled = client.post(
        base + f"/rooms/{room_id}/turns/{event}/cancel", headers=a, json={}
    )
    assert (
        canceled.status_code == 200 and canceled.json()["data"]["phase"] == "cancelled"
    )
    assert transport.generations == 0 and transport.calls == []
    closed = client.post(
        base + f"/rooms/{room_id}/end", headers=a, json={"expected_generation": 1}
    )
    assert closed.status_code == 200


def test_ongoing_version_is_pinned_until_user_resets_to_latest(authenticated_http):
    from copy import deepcopy
    from sqlalchemy import select
    from app.models import ChannelLlmBinding
    from uuid import UUID

    c, client, a, _, transport, _ = authenticated_http
    base, room_id = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room_id)
    http_confirm(authenticated_http, base, room_id, event, view)
    with Session(c.engine) as db:
        old = db.scalar(
            select(ChannelLlmBinding).where(ChannelLlmBinding.room_id == UUID(room_id))
        )
        old_cid = old.provider_conversation_id
        copied = bots.copy_version(
            c.bot_id, c.version_id, _request(), db.get(User, c.user_id), db
        )["data"]
        latest_id = UUID(copied["id"])
        latest = db.get(BotVersion, latest_id)
        doc = deepcopy(latest.version_json)
        doc["dialogs"][0]["llmAnswerPrompt"] = "새 버전 지침"
        doc["dialog_flow_graphs"][0]["nodes"][1]["config"]["basicMessages"] = [
            "새 버전: {{$_llm_answer_text}}"
        ]
        bots._assign_version_document(latest, doc)
        bot = db.get(Bot, c.bot_id)
        assert (
            bots._close_open_channel_rooms_for_version_change(
                db, _request(), db.get(User, c.user_id), bot, c.version_id, latest_id
            )
            == 1
        )
        assert (
            db.get(ChannelRoom, UUID(room_id)).status == "open"
            and db.get(ChannelRoom, c.room_id).status == "closed"
        )
        bot.active_version_id = latest_id
        latest.status = "active"
        db.get(BotVersion, c.version_id).status = "draft"
        db.commit()
    event2, view2 = http_process(
        authenticated_http, base, room_id, "old-followup", "이전 문맥 후속"
    )
    assert view2["message"]["text"] == "답변: 보여줄 답변"
    http_confirm(authenticated_http, base, room_id, event2, view2)
    with Session(c.engine) as db:
        old = db.scalar(
            select(ChannelLlmBinding).where(
                ChannelLlmBinding.room_id == UUID(room_id),
                ChannelLlmBinding.generation == 1,
            )
        )
        assert (
            old.provider_conversation_id == old_cid and old.version_id == c.version_id
        )
    reset = client.post(
        base + f"/rooms/{room_id}/new",
        headers=a,
        json={
            "expected_generation": 1,
            "confirm_reset": True,
            "dialog_id": "pllm-module",
        },
    )
    assert reset.status_code == 200 and reset.json()["data"]["room"][
        "version_id"
    ] == str(latest_id)
    _, view3 = http_process(
        authenticated_http,
        base,
        room_id,
        "latest-question",
        "새 문맥",
        expected_generation=2,
    )
    assert view3["message"]["text"] == "새 버전: 보여줄 답변"
    payload = [
        call[2]
        for call in transport.calls
        if call[0] == "POST" and call[1].endswith("/responses")
    ][-1]
    assert len(payload["input"]) == 1 and "새 버전 지침" in payload["instructions"]
    old_history = client.get(base + f"/rooms/{room_id}?generation=1", headers=a)
    assert (
        old_history.status_code == 200
        and len(old_history.json()["data"]["messages"]) == 4
    )


@pytest.fixture
def admin_http(authenticated_http):
    from app.models import Role, UserRole
    from sqlalchemy import select, delete

    c = authenticated_http[0]
    created = False
    with Session(c.engine) as db:
        role = db.scalar(select(Role).where(Role.code == "system_manager"))
        if role is None:
            role = Role(code="system_manager", name="합성 검증 관리자")
            db.add(role)
            db.flush()
            created = True
        assert role.deleted_at is None
        link = UserRole(user_id=c.user_id, role_id=role.id)
        db.add(link)
        db.commit()
        role_id = role.id
        link_id = link.id
    try:
        yield authenticated_http
    finally:
        with Session(c.engine) as db:
            db.execute(delete(UserRole).where(UserRole.id == link_id))
            if (
                created
                and db.scalar(
                    select(UserRole.id).where(UserRole.role_id == role_id).limit(1)
                )
                is None
            ):
                db.execute(delete(Role).where(Role.id == role_id))
            db.commit()


def test_creator_alone_cannot_force_revoke_version(authenticated_http):
    c, client, a, _, transport, _ = authenticated_http
    result = client.post(
        f"/api/v1/bots/{c.bot_id}/persistent/revoke",
        headers=a,
        json={"version_id": str(c.version_id), "reason": "권한 검증"},
    )
    assert result.status_code == 403 and transport.generations == 0


def test_forced_revoke_blocks_late_result_and_keeps_actual_usage(
    admin_http, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import select
    from app.models import (
        ChannelLlmBinding,
        ChannelLlmVersionGuard,
        LlmBudgetAccount,
        AuditLog,
    )
    from uuid import UUID

    c, client, a, _, transport, _ = admin_http
    base, room_id = http_open(admin_http)
    sent = client.post(
        base + f"/rooms/{room_id}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": "late-revoked",
            "question": "질문",
            "dialog_id": "pllm-module",
        },
    )
    assert sent.status_code == 202
    event_id = sent.json()["data"]["event_id"]
    barrier = Barrier(2)
    original = type(transport).__call__

    def held_response(self, method, url, payload, headers, timeout):
        raw = original(self, method, url, payload, headers, timeout)
        if method == "POST" and url.endswith("/responses"):
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
        return raw

    monkeypatch.setattr(type(transport), "__call__", held_response)

    def worker():
        with Session(c.engine, autoflush=False) as db:
            return bots._process_persistent_llm_events(db, _request(), limit=10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        barrier.wait(timeout=10)
        try:
            revoked = client.post(
                base + "/revoke",
                headers=a,
                json={"version_id": str(c.version_id), "reason": "합성 보안 폐기"},
            )
        finally:
            barrier.wait(timeout=10)
        future.result(timeout=10)
    assert revoked.status_code == 200, revoked.text
    assert client.get(
        base + f"/rooms/{room_id}/turns/{event_id}", headers=a
    ).status_code in {403, 409}
    with Session(c.engine) as db:
        event = db.get(ChannelQueueEvent, UUID(event_id))
        binding = db.get(ChannelLlmBinding, event.binding_id)
        assert (
            event.provider_response_id == "resp_1"
            and event.phase == "revoked"
            and binding.status == "revoked"
        )
        assert db.get(ChannelLlmVersionGuard, c.version_id).mutation_status == "revoked"
        accounts = db.scalars(
            select(LlmBudgetAccount).where(
                LlmBudgetAccount.scope_key.contains(str(c.org_id))
            )
        ).all()
        assert accounts and all(
            row.spent_count == 1 and row.reserved_count == 0 and row.spent_amount > 0
            for row in accounts
        )
        assert (
            db.scalar(
                select(AuditLog.id).where(
                    AuditLog.target_id == c.version_id,
                    AuditLog.action_type == "persistent.version.revoked",
                )
            )
            is not None
        )
        bots._process_persistent_llm_events(db, _request(), limit=10)
    assert transport.generations == 1


def test_readiness_is_server_scoped_and_returns_no_prompt_or_secret(
    authenticated_http, monkeypatch
):
    c, client, a, b, transport, _ = authenticated_http
    base = f"/api/v1/bots/{c.bot_id}/persistent"
    response = client.get(base + "/config", headers=a)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is True and data["ready"] is True
    assert data["profiles"]["pllm-module"]["name"] == "지속대화 샘플"
    assert (
        "고정 지침" not in response.text
        and "test-only-placeholder" not in response.text
    )
    assert client.get(base + "/config", headers=b).status_code == 404
    monkeypatch.setattr(settings, "persistent_llm_execution_enabled", False)
    data = client.get(base + "/config", headers=a).json()["data"]
    assert data["enabled"] is True and data["ready"] is False
    assert transport.generations == 0


def test_owned_room_list_restores_history_without_client_identity(authenticated_http):
    c, client, a, b, transport, _ = authenticated_http
    base, room = http_open(authenticated_http)
    result = client.get(base + "/rooms", headers=a)
    assert result.status_code == 200
    assert [row["id"] for row in result.json()["data"]["rooms"]] == [room]
    assert client.get(base + "/rooms", headers=b).status_code == 404
    assert transport.generations == 0


def test_explicit_reconcile_only_reads_known_response_and_never_generates(
    authenticated_http,
):
    c, client, a, b, transport, _ = authenticated_http
    base, room = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room)
    calls = transport.generations
    result = client.post(
        base + f"/rooms/{room}/turns/{event}/reconcile", headers=a, json={}
    )
    assert result.status_code == 200
    assert result.json()["data"]["phase"] == "pending_delivery"
    assert (
        client.post(
            base + f"/rooms/{room}/turns/{event}/reconcile", headers=b, json={}
        ).status_code
        == 404
    )
    assert transport.generations == calls


def test_force_revoke_keeps_closed_generations_closed_and_denies_old_history(
    admin_http,
):
    from app.models import ChannelLlmBinding
    from uuid import UUID
    from sqlalchemy import select

    c, client, a, _, transport, _ = admin_http
    base, room_id = http_open(admin_http)
    event, view = http_process(admin_http, base, room_id)
    if view["message"] is None:
        with Session(c.engine) as db:
            row = db.get(ChannelQueueEvent, UUID(event))
            assert view["message"], (view, row.error_message, transport.generations)
    http_confirm(admin_http, base, room_id, event, view)
    reset = client.post(
        base + f"/rooms/{room_id}/new",
        headers=a,
        json={
            "expected_generation": 1,
            "confirm_reset": True,
            "dialog_id": "pllm-module",
        },
    )
    assert reset.status_code == 200
    revoked = client.post(
        base + "/revoke",
        headers=a,
        json={"version_id": str(c.version_id), "reason": "복수 generation 보안 폐기"},
    )
    assert revoked.status_code == 200, revoked.text
    assert (
        client.get(base + f"/rooms/{room_id}?generation=1", headers=a).status_code
        == 403
    )
    assert (
        client.get(base + f"/rooms/{room_id}?generation=2", headers=a).status_code
        == 403
    )
    assert client.get(base + "/rooms", headers=a).json()["data"]["rooms"] == []
    with Session(c.engine) as db:
        rows = db.scalars(
            select(ChannelLlmBinding)
            .where(ChannelLlmBinding.room_id == UUID(room_id))
            .order_by(ChannelLlmBinding.generation)
        ).all()
        assert [row.status for row in rows] == ["closed", "revoked"]
    assert transport.generations == 1


@pytest.mark.parametrize("stored_validated", [False, True])
def test_http_background_reconcile_renders_original_talk_once(
    authenticated_http, monkeypatch, stored_validated
):
    from uuid import UUID
    from app.models import ChannelLlmBinding

    c, client, a, _, transport, _ = authenticated_http
    monkeypatch.setattr(settings, "persistent_llm_background", True)
    transport.response_mode = "background"
    base, room = http_open(authenticated_http)
    event, view = http_process(authenticated_http, base, room)
    assert view["phase"] == "response_pending" and view["message"] is None
    if stored_validated:
        with Session(c.engine) as db:
            row = db.get(ChannelQueueEvent, UUID(event))
            runtime = bots._persistent_runtime_service(
                db,
                bots._persistent_scope(db.get(ChannelLlmBinding, row.binding_id)),
                event_id=row.id,
            )
            monkeypatch.setattr(
                runtime,
                "display_transform",
                lambda answer: (_ for _ in ()).throw(
                    RuntimeError("synthetic display outage")
                ),
            )
            runtime.reconcile_turn(
                db,
                bots._persistent_scope(db.get(ChannelLlmBinding, row.binding_id)),
                row.id,
            )
            assert db.get(ChannelQueueEvent, row.id).phase == "validated"
    result = client.post(
        base + f"/rooms/{room}/turns/{event}/reconcile", headers=a, json={}
    )
    assert result.status_code == 200
    final = result.json()["data"]
    assert (
        final["phase"] == "pending_delivery"
        and final["message"]["text"] == "답변: 보여줄 답변"
    )
    assert (
        http_confirm(authenticated_http, base, room, event, final)["phase"]
        == "committed"
    )
    assert transport.generations == 1


def test_stale_end_cannot_archive_room_of_new_generation(
    authenticated_http, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import select
    from uuid import UUID
    from app.services import version_pins
    from app.models import ChannelLlmBinding, User

    c, client, a, _, transport, _ = authenticated_http
    base, room = http_open(authenticated_http)
    barrier = Barrier(2)
    original = version_pins.close_pinned_dialog

    def held_close(db, binding_id):
        if db.info.get("stale_end"):
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
        return original(db, binding_id)

    monkeypatch.setattr(version_pins, "close_pinned_dialog", held_close)

    def end():
        from fastapi import HTTPException

        with Session(c.engine, autoflush=False) as db:
            db.info["stale_end"] = True
            try:
                result = bots.close_persistent_room(
                    c.bot_id,
                    UUID(room),
                    bots.PersistentCloseRequest(expected_generation=1),
                    _request(),
                    db.get(User, c.user_id),
                    db,
                )
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(end)
        barrier.wait(timeout=10)
        try:
            reset = client.post(
                base + f"/rooms/{room}/new",
                headers=a,
                json={
                    "expected_generation": 1,
                    "confirm_reset": True,
                    "dialog_id": "pllm-module",
                },
            )
            assert reset.status_code == 200
        finally:
            barrier.wait(timeout=10)
        status = future.result(timeout=10)
    assert status == 409
    with Session(c.engine) as db:
        active = db.scalar(
            select(ChannelLlmBinding).where(
                ChannelLlmBinding.room_id == UUID(room),
                ChannelLlmBinding.generation == 2,
            )
        )
        assert (
            active.status == "open"
            and db.get(ChannelRoom, UUID(room)).deleted_at is None
        )
    result = client.get(base + f"/rooms/{room}", headers=a)
    assert result.status_code == 200 and result.json()["data"]["room"]["can_send"]
    assert transport.generations == 0


@pytest.mark.parametrize("expired", [False, True])
def test_existing_worker_recovers_only_expired_submitted_without_model_retry(
    authenticated_http, expired
):
    from app.services.llm_budget import mark_turn_submitted
    from app.models import LlmBudgetReservation, ChannelLlmBinding
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from uuid import UUID

    c, client, a, _, transport, _ = authenticated_http
    base, room = http_open(authenticated_http)
    sent = client.post(
        base + f"/rooms/{room}/turns",
        headers=a,
        json={
            "expected_generation": 1,
            "message_key": "interrupted-worker",
            "question": "중단된 질문",
            "dialog_id": "pllm-module",
        },
    )
    assert sent.status_code == 202
    event_id = UUID(sent.json()["data"]["event_id"])
    with Session(c.engine) as db:
        assert mark_turn_submitted(db, event_id)
        event = db.get(ChannelQueueEvent, event_id)
        event.lease_owner = "synthetic-dead-worker"
        event.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=-1 if expired else 120
        )
        fence = event.lease_fence
        db.commit()
    with Session(c.engine) as db:
        bots._process_persistent_llm_events(db, _request(), limit=10)
        event = db.get(ChannelQueueEvent, event_id, populate_existing=True)
        assert event.phase == ("unknown" if expired else "submitted")
        assert event.lease_fence == (fence + 1 if expired else fence)
        rows = db.scalars(
            select(LlmBudgetReservation).where(
                LlmBudgetReservation.event_id == event_id
            )
        ).all()
        assert rows and all(
            row.state == ("unknown" if expired else "submitted") for row in rows
        )
        assert db.get(ChannelLlmBinding, event.binding_id).status == "open"
    assert transport.generations == 0


@pytest.mark.parametrize("kind", ["snapshot", "split"])
def test_admin_backfill_refreshes_registered_version_after_concurrent_edit_and_pin(
    admin_http, monkeypatch, kind
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from copy import deepcopy
    from app.api.routes import admin
    from app.models import (
        Organization,
        BotVersion,
        VersionDialogAsset,
        User,
        ChannelLlmBinding,
    )
    from app.schemas.admin import AdminVersionStorageBackfillRequest
    from app.schemas.bot import VersionUpdateRequest
    from app.services.version_pins import open_pinned_dialog
    from sqlalchemy import select

    c, _, _, _, transport, _ = admin_http
    if kind == "snapshot":
        # backfill의 실제 대상인 누락 파생 자료를 재현한다. 원본 version_json은 유지한다.
        with Session(c.engine) as db:
            db.get(BotVersion, c.version_id).system_config_json = None
            db.commit()
    barrier = Barrier(2)
    original = admin._version_storage_filtered_versions

    def delayed(db, *args, **kwargs):
        rows = original(db, *args, **kwargs)
        if db.info.get("stale_backfill") and not db.info.get("paused"):
            db.info["paused"] = True
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
        return rows

    monkeypatch.setattr(admin, "_version_storage_filtered_versions", delayed)
    monkeypatch.setattr(
        admin, "_get_default_organization", lambda db: db.get(Organization, c.org_id)
    )

    def backfill():
        with Session(c.engine, autoflush=False) as db:
            db.info["stale_backfill"] = True
            handler = (
                admin.backfill_version_read_snapshots
                if kind == "snapshot"
                else admin.backfill_version_storage
            )
            return handler(
                AdminVersionStorageBackfillRequest(bot_id=c.bot_id),
                _request(),
                db,
                db.get(User, c.user_id),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(backfill)
        barrier.wait(timeout=10)
        try:
            with Session(c.engine) as db:
                version = db.get(BotVersion, c.version_id)
                doc = deepcopy(version.version_json)
                doc["system_config"]["instructions"] = "새 고정 지침"
                doc["dialogs"][0]["name"] = "새 고정 지식"
                doc["dialogs"][0]["displayName"] = "새 고정 지식"
                bots.update_version(
                    c.bot_id,
                    c.version_id,
                    VersionUpdateRequest(version_json=doc),
                    _request(),
                    db.get(User, c.user_id),
                    db,
                )
                binding = open_pinned_dialog(
                    db, c.scope, {"instructions": "새 고정 지침"}
                )
                snapshot_hash = binding.snapshot_hash
                binding_id = binding.id
                db.commit()
        finally:
            barrier.wait(timeout=10)
        result = future.result(timeout=10)
    assert (
        result["data"]["processed_versions"] == 1
        and result["data"]["failed_versions"] == []
    )
    with Session(c.engine) as db:
        version = db.get(BotVersion, c.version_id)
        assert version.version_json["system_config"]["instructions"] == "새 고정 지침"
        if kind == "snapshot":
            assert version.system_config_json["instructions"] == "새 고정 지침"
        else:
            row = db.scalar(
                select(VersionDialogAsset).where(
                    VersionDialogAsset.version_id == c.version_id,
                    VersionDialogAsset.dialog_id == "pllm-module",
                    VersionDialogAsset.deleted_at.is_(None),
                )
            )
            assert row.name == "새 고정 지식"
        assert db.get(ChannelLlmBinding, binding_id).snapshot_hash == snapshot_hash
    assert transport.generations == 0
