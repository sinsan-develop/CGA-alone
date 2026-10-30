"""S07 기존 3종의 opt-in 방·질문자 증명·중복 계약. 실제 PG, Provider 대역."""
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.main import app
from app.models import AdminChannel, ChannelRoom, ChannelMessage
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot


@pytest.fixture
def client_context(credential_context):
    c = credential_context
    for code in ("KAKAO", "TEAMS"):
        c[2].add(AdminChannel(organization_id=c[0].organization_id, code=code, name="S07 QA " + code, status="active"))
    c[2].flush()
    client = TestClient(app)
    try:
        yield c, client
    finally:
        client.close()


def create(context, channel="webchat", mode="shared", kind="user", external=None):
    c, client = context
    body = {"bot_id": str(c[0].id), "client_room_id": external or "s07-" + uuid4().hex,
        "participant_id": "untrusted-client-actor", "participant_name": "질문자", "start_immediately": False}
    if mode is not None:
        body.update(room_mode=mode, participant_kind=kind)
    response = client.post(f"/api/v1/channels/{channel}/rooms", json=body, headers={"X-Aidot-Webchat-Key": "s04-qa-key"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def member_headers(room, event=None):
    headers = {"X-Aidot-Webchat-Key": "s04-qa-key", "X-Aidot-Conversation-Token": room["conversationToken"]}
    if event is not None:
        headers["X-Aidot-Event-Id"] = event
    return headers


def send(context, room, text="질문", channel="webchat", addressed=True, event=None, **extra):
    return context[1].post(f"/api/v1/channels/{channel}/rooms/{room['room']['id']}/messages",
        headers=member_headers(room, event), json={"message": text, "addressed": addressed, **extra})


@pytest.mark.parametrize("channel", ["webchat", "kakao", "ms-teams"])
def test_legacy_payload_still_works(client_context, channel):
    c, client = client_context
    room = create(client_context, channel, mode=None)
    assert "conversationToken" not in room
    response = client.post(f"/api/v1/channels/{channel}/rooms/{room['room']['id']}/messages",
        headers={"X-Aidot-Webchat-Key": "s04-qa-key"}, json={"message": "기존 질문", "participant_id": "legacy-actor"})
    assert response.status_code == 200
    assert response.json()["data"]["botMessage"]["text"] == "추가 설명"
    assert not c[3]


@pytest.mark.parametrize("channel", ["webchat", "kakao", "ms-teams"])
def test_questioners_need_no_creator_login(client_context, channel):
    c, client = client_context
    room = create(client_context, channel)
    assert room["conversationToken"] and room["participantId"] != "untrusted-client-actor"
    assert room["participantKind"] == "user"
    path = f"/api/v1/channels/{channel}/rooms/{room['room']['id']}"
    assert client.get(path, headers={"X-Aidot-Webchat-Key": "s04-qa-key"}).status_code == 404
    response = client.get(path, headers=member_headers(room))
    assert response.status_code == 200
    assert response.json()["data"]["turn"]["canAskBot"] is True
    assert not c[3]


def test_bot_and_llm_session_actor_binding(client_context):
    c, _ = client_context
    room = create(client_context, kind="bot")
    assert room["participantKind"] == "bot"
    first = send(client_context, room, "다른 봇의 질문", participant_id="spoofed", participant_kind="user")
    assert first.status_code == 200
    data = first.json()["data"]
    assert data["userMessage"]["participantKind"] == "bot"
    assert data["userMessage"]["participantId"] == room["participantId"]
    stored = c[2].get(ChannelRoom, UUID(room["room"]["id"]))
    assert "다른 봇의 질문" in stored.metadata_json["conversationHistory"]["user_raw_utterances"]
    assert send(client_context, room, "후속 답변").status_code == 200
    assert len(c[3]) == 1


def test_named_bot_call_targets_connected_bot(client_context):
    c, _ = client_context
    room = create(client_context)
    response = send(client_context, room, "@" + c[0].name + " 질문", bot_id=str(uuid4()), participant_id="spoofed")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["bot"]["id"] == str(c[0].id)
    assert data["userMessage"]["participantId"] == room["participantId"]
    assert data["botMessage"]["text"] == "추가 설명"


@pytest.mark.parametrize("channel", ["webchat", "kakao", "ms-teams"])
def test_name_in_general_chat_is_not_implicit_call(client_context, channel):
    c, _ = client_context
    room = create(client_context, channel)
    response = send(client_context, room, c[0].name + "에 대해 이야기", channel, addressed=False)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["botMessage"] is None and data["botMessages"] == []
    assert data["turn"]["state"] == "idle" and not c[3]


def test_duplicate_event_runs_once(client_context):
    c, _ = client_context
    room = create(client_context, mode="private")
    first = send(client_context, room, "처음 질문", event="event-1")
    assert first.status_code == 200
    repeated = send(client_context, room, "처음 질문", event="event-1")
    assert repeated.status_code == 200
    assert repeated.json()["data"]["userMessage"]["id"] == first.json()["data"]["userMessage"]["id"]
    assert not c[3]
    answer = send(client_context, room, "후속 설명", event="event-2")
    assert answer.status_code == 200 and len(c[3]) == 1
    duplicate = send(client_context, room, "후속 설명", event="event-2")
    assert duplicate.status_code == 200 and len(c[3]) == 1
    assert duplicate.json()["data"]["botMessage"]["id"] == answer.json()["data"]["botMessage"]["id"]
    assert send(client_context, room, "다른 내용", event="event-2").status_code == 409
    assert c[2].scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id == UUID(room["room"]["id"]))) == 4


def test_same_actor_in_different_rooms_is_isolated(client_context):
    c, client = client_context
    first = create(client_context, mode="private", external="room-A")
    second = create(client_context, mode="private", external="room-B")
    assert first["participantId"] != second["participantId"]
    assert send(client_context, first, "A 질문").status_code == 200
    assert send(client_context, second, "B 질문").status_code == 200
    assert send(client_context, first, "A 후속").status_code == 200
    result = client.get(f"/api/v1/channels/webchat/rooms/{second['room']['id']}", headers=member_headers(second))
    assert result.status_code == 200
    assert result.json()["data"]["runtime"]["waitingNodeId"] == "followup"
    assert len(c[3]) == 1


@pytest.mark.parametrize("case", ["wrong-room", "wrong-channel", "missing-proof", "expired", "closed", "wrong-org"])
def test_common_credential_rejects_spoofing(client_context, case):
    from copy import deepcopy
    c, client = client_context
    room = create(client_context)
    path = f"/api/v1/channels/webchat/rooms/{room['room']['id']}/messages"
    headers = member_headers(room)
    stored = c[2].get(ChannelRoom, UUID(room["room"]["id"]))
    if case == "wrong-room":
        other = create(client_context)
        headers = member_headers(other)
    if case == "wrong-channel": path = path.replace("/webchat/", "/kakao/")
    if case == "missing-proof": headers.pop("X-Aidot-Conversation-Token")
    if case == "closed": stored.status = "closed"
    if case in {"expired", "wrong-org"}:
        metadata = deepcopy(stored.metadata_json)
        if case == "expired": metadata["__webchatAccess"]["members"][room["participantId"]]["expires_at"] = "2000-01-01T00:00:00+00:00"
        else: metadata["__webchatAccess"]["organization_id"] = str(uuid4())
        stored.metadata_json = metadata
    c[2].flush()
    before = c[2].scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id == stored.id))
    response = client.post(path, headers=headers, json={"message": "침입", "addressed": True, "participant_id": room["participantId"]})
    assert response.status_code == 404
    assert not c[3]
    assert c[2].scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id == stored.id)) == before


def test_shared_join_and_questioner_turn_are_bound(client_context):
    c, client = client_context
    room = create(client_context)
    joined = client.post(f"/api/v1/channels/webchat/rooms/{room['room']['id']}/participants",
        headers={"X-Aidot-Webchat-Key": "s04-qa-key"}, json={"participant_name": "다른 봇", "participant_kind": "bot"})
    assert joined.status_code == 200
    other = {**joined.json()["data"], "room": room["room"]}
    assert other["participantKind"] == "bot" and other["participantId"] != room["participantId"]
    assert send(client_context, room).status_code == 200
    assert send(client_context, other, "대신 답변", participant_id=room["participantId"]).status_code == 409
    general = send(client_context, other, "일반 이야기", addressed=False)
    assert general.status_code == 200 and general.json()["data"]["userMessage"]["participantKind"] == "bot"
    assert send(client_context, room, "원래 질문자의 답변").status_code == 200
    assert len(c[3]) == 1
    private = create(client_context, mode="private")
    assert client.post(f"/api/v1/channels/webchat/rooms/{private['room']['id']}/participants",
        headers={"X-Aidot-Webchat-Key": "s04-qa-key"}, json={"participant_name": "추가"}).status_code == 404


def test_unknown_event_is_not_reexecuted(client_context, monkeypatch):
    from app.api.routes import channels
    c, client = client_context
    room = create(client_context, mode="private")
    assert send(client_context, room, "시작").status_code == 200
    original = channels.create_channel_room_message
    executions = []
    def interrupted(*args, **kwargs):
        executions.append(True)
        original(*args, **kwargs)
        raise TimeoutError("synthetic unknown result")
    monkeypatch.setattr(channels, "create_channel_room_message", interrupted)
    # 실제 route는 이미 등록된 원 함수를 사용하고, 내부 runtime 호출만 중단한다.
    with pytest.raises(TimeoutError, match="synthetic unknown result"):
        send(client_context, room, "외부 실행", event="unknown-1")
    assert len(executions) == 1 and len(c[3]) == 1
    retry = send(client_context, room, "외부 실행", event="unknown-1")
    assert retry.status_code == 409 and len(executions) == 1 and len(c[3]) == 1
    stored = c[2].get(ChannelRoom, UUID(room["room"]["id"]))
    assert next(iter(stored.metadata_json["__clientEvents"].values()))["status"] == "unknown"


def test_completed_duplicate_has_current_turn_and_member_scope(client_context):
    c, client = client_context
    room = create(client_context)
    assert send(client_context, room, "시작", event="same-event").status_code == 200
    assert send(client_context, room, "마침", event="finish").status_code == 200
    repeat = send(client_context, room, "시작", event="same-event")
    assert repeat.status_code == 200 and repeat.json()["data"]["turn"]["state"] == "idle"
    joined = client.post(f"/api/v1/channels/webchat/rooms/{room['room']['id']}/participants",
        headers={"X-Aidot-Webchat-Key": "s04-qa-key"}, json={"participant_name": "B"}).json()["data"]
    other = {**joined, "room": room["room"]}
    assert send(client_context, other, "B 이야기", addressed=False, event="same-event").status_code == 200
    assert len(c[3]) == 1


from tests.cga_alone.test_shared_webchat import committed_shared_context
from tests.cga_alone.test_room_turn import turn_context


def test_parallel_duplicate_and_reconnect_use_persisted_event(committed_shared_context):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import Session
    c = committed_shared_context
    base = "/api/v1/channels/webchat/rooms"
    response = c.client.post(base, headers=c.headers, json={"bot_id": str(c.bot_id), "room_mode": "shared", "participant_name": "A"})
    assert response.status_code == 200
    room = response.json()["data"]
    path = base + "/" + room["roomId"] + "/messages"
    headers = {**c.headers, "X-Aidot-Conversation-Token": room["conversationToken"]}
    assert c.client.post(path, headers=headers, json={"message": "A 질문", "addressed": True}).status_code == 200
    event_headers = {**headers, "X-Aidot-Event-Id": "parallel-event"}
    body = {"message": "A 후속", "addressed": True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c.client.post, path, headers=event_headers, json=body)
        try:
            assert c.entered.wait(5)
            duplicate = c.client.post(path, headers=event_headers, json=body)
            assert duplicate.status_code == 409
            assert c.client.post(path, headers=headers, json={"message": "일반 이야기", "addressed": False}).status_code == 200
            with Session(c.engine) as db:
                stored = db.get(ChannelRoom, UUID(room["roomId"]))
                assert db.scalar(select(ChannelRoom).where(ChannelRoom.id == stored.id).with_for_update(nowait=True)) is not None
                entries = stored.metadata_json["__clientEvents"]
                assert len(entries) == 1 and next(iter(entries.values()))["status"] == "processing"
        finally: c.release.set()
        original = future.result(timeout=8)
    assert original.status_code == 200, original.text
    # 새 HTTP 연결/새 DB Session에서 저장된 완료 이벤트를 조회한다.
    other_client = TestClient(app)
    try: repeated = other_client.post(path, headers=event_headers, json=body)
    finally: other_client.close()
    assert repeated.status_code == 200
    assert repeated.json()["data"]["botMessage"]["id"] == original.json()["data"]["botMessage"]["id"]
    assert c.http_calls == ["A 질문"]
    with Session(c.engine) as db:
        stored = db.get(ChannelRoom, UUID(room["roomId"]))
        assert len(stored.metadata_json["__clientEvents"]) == 1
        assert next(iter(stored.metadata_json["__clientEvents"].values()))["status"] == "completed"
        assert db.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.room_id == stored.id)) == 5
