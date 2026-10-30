"""S06 시뮬레이터가 소비하는 기존 개인/공유 HTTP 계약의 실제 PG 검증."""
import pytest
from app.core.config import settings
from fastapi.testclient import TestClient
from app.main import app
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.cga_alone.test_two_stage_runtime import native_secondary_document


@pytest.fixture(autouse=True)
def no_background_workers(monkeypatch):
    # TestClient lifespan이 QA HTTP 계약과 무관한 기본 DB worker를 시작하지 않게 한다.
    for name in ("channel_queue_worker_enabled", "edit_lock_cleanup_enabled", "nlu_training_worker_enabled"):
        monkeypatch.setattr(settings, name, False)


def test_shared_simulator_two_participants_keep_bot_and_general_inputs_separate(credential_context):
    c = credential_context
    with TestClient(app) as client:
        base = f"/api/v1/webchat/bots/{c[0].id}"
        key = {"X-Aidot-Webchat-Key": "s04-qa-key"}
        def data(response):
            assert response.status_code == 200, response.text
            return response.json()["data"]
        a = data(client.post(base + "/shared-rooms", headers=key, json={"participant_name": "참여자 A"}))
        b = data(client.post(base + f"/shared-rooms/{a['roomId']}/participants", headers=key, json={"participant_name": "참여자 B"}))
        expected_version = {"id": str(c[1].id), "name": c[1].name, "versionNo": c[1].version_no}
        assert a["activeVersion"] == expected_version
        assert a["runtime"]["waitingNodeId"] is None
        assert b["activeVersion"] == expected_version
        def headers(member):
            return {**key, "X-Aidot-Conversation-Token": member["conversationToken"]}
        def send(member, text, addressed):
            return client.post(base + f"/rooms/{member['roomId']}/messages", headers=headers(member), json={"message": text, "addressed": addressed})
        def history(member):
            return data(client.get(base + f"/shared-rooms/{member['roomId']}/messages", headers=headers(member)))
        assert data(send(a, "첫 질문", True))["botMessage"]["text"] == "추가 설명"
        assert history(a)["turn"]["canAskBot"] is True
        assert history(a)["runtime"]["waitingNodeId"] == "followup"
        assert history(b)["runtime"]["waitingNodeId"] == "followup"
        assert history(a)["activeVersion"] == expected_version
        assert history(b)["turn"]["canAskBot"] is False
        before = history(b)["messages"]
        assert send(b, "대신 이어가기", True).status_code == 409
        assert history(b)["messages"] == before and c[3] == []
        assert data(send(b, "일반 대화", False))["botMessage"] is None
        saved = history(a)
        assert saved["messages"][-1]["participantId"] == b["participantId"]
        assert saved["messages"][-1]["participantName"] == "참여자 B"
        assert saved["turn"]["ownerParticipantId"] == a["participantId"]
        assert data(send(a, "후속 설명", True))["runtime"]["dialogEnded"] is True
        assert history(b)["turn"]["canAskBot"] is True
        assert history(b)["runtime"]["waitingNodeId"] is None
        assert len(c[3]) == 1
        # 재접속은 동일 증명의 GET만 수행하며 질문/API를 재전송하지 않는다.
        restored = history(b)
        assert restored["messages"] == history(a)["messages"]
        assert len(c[3]) == 1
        assert b["conversationToken"] not in str(restored)


def test_simulator_private_wire_keeps_existing_token_followup(credential_context):
    c = credential_context
    with TestClient(app) as client:
        url = f"/api/v1/webchat/bots/{c[0].id}/rooms/simulator-private/messages"
        key = {"X-Aidot-Webchat-Key": "s04-qa-key"}
        first = client.post(url, headers=key, json={"message": "질문"})
        assert first.status_code == 200
        result = first.json()["data"]
        assert result["botMessage"]["text"] == "추가 설명"
        assert result["runtime"]["waitingNodeId"] == "followup"
        assert result["activeVersion"]["id"] == str(c[1].id)
        token = result["conversationToken"]
        second = client.post(url, headers={**key, "X-Aidot-Conversation-Token": token}, json={"message": "후속 설명"})
        assert second.status_code == 200
        assert second.json()["data"]["botMessage"]["text"] == "답변: 외부 생성 답변"
        assert len(c[3]) == 1


def test_shared_history_reports_room_version_before_next_question_applies_latest(credential_context):
    from copy import deepcopy
    from app.models import BotVersion
    from tests.cga_alone.test_shared_webchat import create, history, send_shared
    c = credential_context
    a = create(c)
    send_shared(c, a)
    old = history(c, a["roomId"], a["conversationToken"])
    newer = BotVersion(bot_id=c[0].id, version_no=c[1].version_no + 1, name="새 운영 버전",
        created_by=c[0].created_by, version_json=deepcopy(c[1].version_json), status="active")
    newer.system_config_json = deepcopy(c[1].system_config_json)
    c[2].add(newer)
    c[2].flush()
    c[0].active_version_id = newer.id
    c[2].flush()
    current = history(c, a["roomId"], a["conversationToken"])
    assert current["activeVersion"] == old["activeVersion"]
    assert current["activeVersion"]["id"] == str(c[1].id)
    assert current["runtime"]["waitingNodeId"] == "followup"
    assert len(c[3]) == 0
    assert set(current["runtime"]) == {"dialogEnded", "sessionEnded", "completionReason", "waitingNodeId"}
    assert set(current["activeVersion"]) == {"id", "name", "versionNo"}
