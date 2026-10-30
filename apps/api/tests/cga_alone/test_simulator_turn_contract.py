"""S06 시뮬레이터가 소비하는 기존 개인/공유 HTTP 계약의 실제 PG 검증."""
from fastapi.testclient import TestClient
from app.main import app
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_secondary_llm_config import secondary_bot
from tests.cga_alone.test_two_stage_runtime import native_secondary_document


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
        def headers(member):
            return {**key, "X-Aidot-Conversation-Token": member["conversationToken"]}
        def send(member, text, addressed):
            return client.post(base + f"/rooms/{member['roomId']}/messages", headers=headers(member), json={"message": text, "addressed": addressed})
        def history(member):
            return data(client.get(base + f"/shared-rooms/{member['roomId']}/messages", headers=headers(member)))
        assert data(send(a, "첫 질문", True))["botMessage"]["text"] == "추가 설명"
        assert history(a)["turn"]["canAskBot"] is True
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
        token = result["conversationToken"]
        second = client.post(url, headers={**key, "X-Aidot-Conversation-Token": token}, json={"message": "후속 설명"})
        assert second.status_code == 200
        assert second.json()["data"]["botMessage"]["text"] == "답변: 외부 생성 답변"
        assert len(c[3]) == 1
