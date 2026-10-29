"""전송 대역으로만 OpenAI protocol을 확인한다. 실제 Provider 성공 증거가 아니다."""

from copy import deepcopy
import json
import pytest
from app.services.llm_client import LlmProviderConfig
from app.services.openai_conversations import (
    OpenAIConversations,
    CanonicalItem,
    ProviderProtocolError,
    ProviderTransportError,
    build_detached_payload,
    validated_answer,
    canonical_hash,
)

CONFIG = LlmProviderConfig(
    provider="chatgpt",
    model="registered-test-model",
    base_url="https://api.openai.invalid/v1",
    api_key="test-only-placeholder",
    timeout_seconds=7,
)


class ScriptedTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append((method, url, deepcopy(payload), dict(headers), timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def response(answer="보여줄 답변"):
    return {
        "id": "resp_test",
        "status": "completed",
        "model": CONFIG.model,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"answer": answer}, ensure_ascii=False),
                    }
                ],
            }
        ],
    }


def item(identifier, role, text):
    return {
        "id": identifier,
        "type": "message",
        "role": role,
        "status": "completed",
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text}
        ],
    }


def test_detached_response_never_auto_appends_rag_or_raw_json():
    t = ScriptedTransport(response())
    client = OpenAIConversations(CONFIG, transport=t)
    history = [
        CanonicalItem("msg_u", "user", "이전 질문"),
        CanonicalItem("msg_a", "assistant", "이전 표시 답변"),
    ]
    payload = build_detached_payload(
        CONFIG,
        history,
        instructions="고정 지침",
        question="이번 질문",
        turn_context={"references": ["당회 내부 RAG"]},
        output_tokens=50,
    )
    raw = client.generate_detached(payload, request_id="trace-only")
    assert validated_answer(raw) == "보여줄 답변"
    method, url, body, headers, timeout = t.calls[0]
    assert (method, url, timeout) == ("POST", CONFIG.base_url + "/responses", 7)
    assert "conversation" not in body and "previous_response_id" not in body
    assert body["model"] == CONFIG.model and body["max_output_tokens"] == 50
    assert body["input"][:2] == [
        {"role": "user", "content": "이전 질문"},
        {"role": "assistant", "content": "이전 표시 답변", "phase": "final_answer"},
    ]
    assert "당회 내부 RAG" in body["input"][-1]["content"]
    assert "고정 지침" in body["instructions"] and body["text"]["format"] == {
        "type": "json_object"
    }
    assert headers["X-Client-Request-Id"] == "trace-only"
    assert "Idempotency-Key" not in headers


def test_list_items_is_complete_chronological_and_preserves_ids():
    t = ScriptedTransport(
        {"data": [item("msg_u", "user", "질문")], "has_more": True, "last_id": "msg_u"},
        {
            "data": [item("msg_a", "assistant", "답변")],
            "has_more": False,
            "last_id": "msg_a",
        },
    )
    result = OpenAIConversations(CONFIG, transport=t).list_items("conv_test")
    assert result == [
        CanonicalItem("msg_u", "user", "질문"),
        CanonicalItem("msg_a", "assistant", "답변"),
    ]
    assert "order=asc" in t.calls[0][1] and "limit=100" in t.calls[0][1]
    assert "after=msg_u" in t.calls[1][1]
    assert canonical_hash(result) != canonical_hash(list(reversed(result)))


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "reasoning", "id": "rs_private", "content": []},
        {"type": "message", "id": "msg_system", "role": "system", "content": "hidden"},
        {
            "type": "message",
            "id": "msg_partial",
            "role": "assistant",
            "status": "in_progress",
            "content": "partial",
        },
    ],
)
def test_noncanonical_or_partial_items_fail_closed(bad):
    t = ScriptedTransport({"data": [bad], "has_more": False})
    with pytest.raises(ProviderProtocolError):
        OpenAIConversations(CONFIG, transport=t).list_items("conv_test")


def test_ambiguous_post_is_not_retried():
    t = ScriptedTransport(TimeoutError("synthetic timeout"), response())
    c = OpenAIConversations(CONFIG, transport=t)
    payload = build_detached_payload(
        CONFIG,
        [],
        instructions="지침",
        question="질문",
        turn_context={},
        output_tokens=20,
    )
    with pytest.raises(ProviderTransportError):
        c.generate_detached(payload, request_id="once")
    assert len(t.calls) == 1


def test_append_contains_only_canonical_question_and_display_text():
    t = ScriptedTransport(
        {
            "data": [
                item("msg_u", "user", "질문"),
                item("msg_a", "assistant", "최종 표출문"),
            ]
        }
    )
    OpenAIConversations(CONFIG, transport=t).append_pair(
        "conv_test", "질문", "최종 표출문", request_id="append-once"
    )
    assert t.calls[0][2] == {
        "items": [
            {"type": "message", "role": "user", "content": "질문"},
            {
                "type": "message",
                "role": "assistant",
                "content": "최종 표출문",
                "phase": "final_answer",
            },
        ]
    }


@pytest.mark.parametrize(
    "text",
    ["not json", '{"answer":""}', '{"answer":123}', '{"answer":"a","hidden":"b"}'],
)
def test_invalid_json_has_no_validated_canonical_answer(text):
    raw = response()
    raw["output"][0]["content"][0]["text"] = text
    with pytest.raises(ProviderProtocolError):
        validated_answer(raw)


def test_cancel_requires_known_background_response():
    t = ScriptedTransport({"id": "resp_test", "status": "cancelled"})
    c = OpenAIConversations(CONFIG, transport=t)
    with pytest.raises(ProviderProtocolError):
        c.cancel_response("resp_test", background=False, request_id="cancel")
    assert t.calls == []
    assert (
        c.cancel_response("resp_test", background=True, request_id="cancel")["status"]
        == "cancelled"
    )


def test_other_provider_is_not_automatically_switched_to_openai():
    with pytest.raises(ProviderProtocolError):
        OpenAIConversations(
            LlmProviderConfig("groq", "registered", "https://example.invalid"),
            transport=ScriptedTransport(),
        )


def test_invalid_output_part_reports_protocol_error():
    raw = response()
    raw["output"][0]["content"] = ["not a content object"]
    with pytest.raises(ProviderProtocolError):
        validated_answer(raw)
