from importlib import import_module
from pathlib import Path
import json
from uuid import uuid4
import pytest
from fastapi import HTTPException

FIXTURES=Path(__file__).parent/"cga_alone/fixtures/client_templates"
PROVIDERS=[json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json")) if p.stem in {"slack","discord","telegram","google-chat","line"}]


def bridge():
    return import_module("app.services.channel_connection_bridge")


@pytest.mark.parametrize("fixture",PROVIDERS,ids=lambda x:x["channel"])
def test_normalize_uses_existing_sdk_in_real_node_process(fixture):
    value=bridge().convert_channel_packet(fixture["channel"],"normalize",{"input":fixture["addressed"],"connection":fixture["connection"]})
    assert value and value["addressed"] is True
    assert value["roomMode"] in {"private","shared"} and value["participantKind"] in {"user","bot"}
    assert value["eventId"] and value["nativeRoomKey"] and value["nativeParticipantKey"]
    assert "replyToken" not in value


@pytest.mark.parametrize("fixture",PROVIDERS,ids=lambda x:x["channel"])
def test_reply_uses_native_sdk_without_model_or_platform_call(fixture):
    channel=fixture["channel"];c=fixture["connection"]
    dest={"slack":{"channelId":"CQA001"},"discord":{"channelId":c.get("channelId")},"telegram":{"chatId":c.get("chatId")},"google-chat":{"spaceName":c.get("spaceName")},"line":{}}[channel]
    value=bridge().convert_channel_packet(channel,"replies",{"messages":[{"id":str(uuid4()),"text":"C02 고정 답변","messageType":"text","participantId":"bot","participantKind":"bot","participantName":"bot","createdAt":"2026-11-02T00:00:00Z"}],"destination":dest})
    assert len(value)==1 and "C02 고정 답변" in json.dumps(value,ensure_ascii=False)


def test_bridge_scrubs_native_secrets_and_does_not_pass_process_environment(monkeypatch):
    b=bridge()
    monkeypatch.setenv("OPENAI_API_KEY","synthetic-never-to-child")
    safe=b.sanitize_native_payload({"token":"synthetic-secret","event":{"text":"질문","replyToken":"native-sensitive"},"authorization":"Bearer sensitive"})
    assert "synthetic-secret" not in str(safe) and "native-sensitive" not in str(safe)
    assert "OPENAI_API_KEY" not in b.bridge_environment()


def test_invalid_bridge_input_and_sdk_errors_return_fixed_error_without_echo():
    b=bridge()
    for provider,op,payload in [("constructor","normalize",{}),("slack","exec",{}),("slack","normalize",{"input":{"token":"secret-value"},"connection":{}})]:
        with pytest.raises(HTTPException) as e:b.convert_channel_packet(provider,op,payload)
        assert "secret-value" not in str(e.value.detail)


def test_answer_table_columns_named_token_are_preserved():
    message={"id":str(uuid4()),"text":"표 답변","messageType":"table","payload":{"columns":["token"],"rows":[{"token":"visible-answer"}],"keyColumn":"token","sourceTalkNodeId":"talk-1"}}
    value=bridge().convert_channel_packet("slack","replies",{"messages":[message],"destination":{"channelId":"CQA001"}})
    assert "visible-answer" in json.dumps(value)


def test_reply_packet_rejects_extra_server_credential_fields():
    with pytest.raises(HTTPException):bridge().convert_channel_packet("slack","replies",{"messages":[],"destination":{},"bot_token":"server-synthetic-private"})


@pytest.mark.parametrize("fixture",[x for x in PROVIDERS if x["channel"]!="slack"],ids=lambda x:x["channel"])
def test_compact_button_restores_original_value_using_same_sdk(fixture):
    source={"id":str(uuid4()),"participantKind":"bot","text":"선택","messageType":"button","payload":{"options":["확인","다음"],"sourceTalkNodeId":"talk-1"}}
    ev=bridge().convert_channel_packet(fixture["channel"],"normalize",{"input":fixture["addressed"],"connection":fixture["connection"]})
    ev.update(sourceMessageId=source["id"],choiceIndex=1)
    wire=json.loads(bridge().convert_channel_packet(fixture["channel"],"selection",{"source":source,"event":ev}))
    assert wire["sourceMessageId"]==source["id"] and wire["sourceTalkNodeId"]=="talk-1"
    assert wire["response"]["buttonValue"]=="다음"
    with pytest.raises(HTTPException):bridge().convert_channel_packet(fixture["channel"],"selection",{"source":source,"event":{**ev,"choiceIndex":999}})
    with pytest.raises(HTTPException):bridge().convert_channel_packet(fixture["channel"],"selection",{"source":{**source,"participantKind":"user"},"event":ev})
