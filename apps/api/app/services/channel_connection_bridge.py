"""인증정보와 네트워크 없이 기존 TypeScript SDK 변환을 재사용한다."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
from fastapi import HTTPException
from app.core.config import ROOT_DIR, settings

_PROVIDERS = {"ms-teams", "slack", "discord", "telegram", "google-chat", "line"}
_SECRET_KEYS = {"authorization", "token", "bottoken", "apptoken", "accesstoken", "clientsecret", "signingsecret", "channelsecret", "privatekey", "serviceaccountjson", "conversationtoken"}


def sanitize_native_payload(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = key.replace("_", "").replace("-", "").lower()
            if normalized in _SECRET_KEYS:
                continue
            # LINE의 일회용 회신 증명은 adapter가 별도 보호 보관한다.
            result[key] = "cga-bridge-placeholder" if normalized == "replytoken" else sanitize_native_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_native_payload(item) for item in value]
    return value


def bridge_environment() -> dict[str, str]:
    # API/DB/provider 비밀값과 NODE_OPTIONS 등의 실행 주입을 상속하지 않는다.
    return {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG") if name in os.environ}


def _prepare_payload(provider: str, operation: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError()
    allowed = {"input", "connection", "eventIndex"} if operation == "normalize" else {"source", "event"} if operation == "selection" else {"messages", "destination"}
    if set(payload) - allowed:
        raise ValueError()
    if operation == "normalize":
        fields = {
            "slack": {"teamId", "botUserId", "botId"},
            "discord": {"applicationId", "botUserId", "guildId", "channelId", "channelType"},
            "telegram": {"botUserId", "botUsername", "chatId", "chatType"},
            "google-chat": {"botUserName", "spaceName", "spaceType"},
            "line": {"botUserId", "sourceType", "sourceId"},
        }[provider]
        connection = payload.get("connection")
        if not isinstance(connection, dict) or set(connection) - fields:
            raise ValueError()
        return {**payload, "input": sanitize_native_payload(payload.get("input"))}
    if operation == "selection":
        source,event=payload.get("source"),payload.get("event")
        if not isinstance(source,dict) or set(source)-{"id","participantId","participantKind","participantName","messageType","text","payload","createdAt"} or not isinstance(event,dict):
            raise ValueError()
        return {"source":source,"event":sanitize_native_payload(event)}
    destination = payload.get("destination", {})
    if not isinstance(destination, dict) or set(destination) - {"channelId", "threadTs", "chatId", "threadId", "spaceName", "threadName"}:
        raise ValueError()
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(message, dict) or set(message) - {"id", "participantId", "participantKind", "participantName", "messageType", "text", "payload", "createdAt"} for message in messages):
        raise ValueError()
    # 사용자 답변의 열 이름·내용은 원문 그대로 보존한다.
    return payload


def convert_channel_packet(provider: str, operation: str, payload: dict):
    provider = provider.replace("_", "-")
    if provider not in _PROVIDERS or operation not in {"normalize", "replies", "selection"}:
        raise HTTPException(422, "CHANNEL_PACKET_INVALID")
    try:
        data = json.dumps({"provider": provider, "operation": operation, "payload": _prepare_payload(provider, operation, payload)}, ensure_ascii=False)
        if len(data.encode("utf-8")) > 1048576:
            raise ValueError()
        executable = shutil.which(settings.channel_connection_node_binary)
        if executable is None:
            raise HTTPException(503, "CHANNEL_PACKET_RUNTIME_UNAVAILABLE")
        script = ROOT_DIR / "packages/shared/src/channel-bridge.mjs"
        result = subprocess.run([executable, str(script)], input=data, text=True, encoding="utf-8", capture_output=True,
            timeout=5, cwd=ROOT_DIR, env=bridge_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        response = json.loads(result.stdout) if len(result.stdout) <= 2097152 else {}
        if result.returncode != 0 or response.get("ok") is not True:
            raise ValueError()
        value = response["value"]
        if operation == "normalize" and isinstance(value, dict):
            value.pop("replyToken", None)
        return value
    except HTTPException:
        raise
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        # child stderr와 입력은 Secret/사용자 질문을 포함할 수 있어 반환·기록하지 않는다.
        raise HTTPException(422, "CHANNEL_PACKET_INVALID") from None
