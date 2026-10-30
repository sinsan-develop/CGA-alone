from __future__ import annotations

import re
import hashlib
import secrets
import hmac
import logging
from contextlib import contextmanager
from threading import Event, Thread
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import SessionLocal
from app.core.config import settings
from app.core.responses import success_response
from app.core.version_documents import normalize_version_document
from app.models import AdminChannel, Bot, BotVersion, Group, ChannelRoom, ChannelMessage, ChannelQueueEvent
from app.services.default_messages import resolve_default_message_text
from app.services.bot_ai_policy import runtime_block_reason, secondary_llm_block_reason
from app.services.llm_client import LlmClientError
from app.services.llm_nlu import classify_intent_with_llm_snapshot
from app.services.scenario_validation import scenario_validation_block_reason, scenario_validation_from_version
from app.services.vector_search import IntentVectorSearchClient, VectorSearchError, intent_vector_config


router = APIRouter(prefix="/webchat", tags=["webchat"])
logger = logging.getLogger(__name__)


class WebchatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    participant_id: str | None = Field(default=None, max_length=120)
    addressed: bool | None = None


class WebchatSharedParticipantRequest(BaseModel):
    participant_name: str = Field(default="사용자", min_length=1, max_length=120)


def _verify_webchat_key(api_key: str | None) -> None:
    expected_key = settings.webchat_api_key.strip()
    if not expected_key:
        return
    if not api_key or api_key.strip() != expected_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="webchat API 인증이 필요합니다.")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _version_ai_config(bot: Bot, version: BotVersion) -> dict[str, Any]:
    data_json = _as_dict(getattr(bot, "data_json", None))
    config = dict(data_json)
    system_config = getattr(version, "system_config_json", None)
    if not isinstance(system_config, dict):
        version_json = normalize_version_document(getattr(version, "version_json", None))
        system_config = version_json.get("system_config")
    ai_config = _as_dict(system_config.get("ai_config") if isinstance(system_config, dict) else None)
    for key, value in ai_config.items():
        if isinstance(config.get(key), dict) and isinstance(value, dict):
            config[key] = {**_as_dict(config.get(key)), **value}
        else:
            config[key] = value
    return config


def _nlu_training_state(version: BotVersion) -> dict[str, Any]:
    snapshot = getattr(version, "nlu_training_json", None)
    if isinstance(snapshot, dict):
        return snapshot
    version_json = normalize_version_document(getattr(version, "version_json", None))
    system_config = _as_dict(version_json.get("system_config"))
    return _as_dict(system_config.get("nlu_training"))


def _dictionary_terms_from_version(version_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    document = normalize_version_document(version_json)
    terms: list[dict[str, Any]] = []
    for item in _as_list(document.get("dictionary")):
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        synonyms = item.get("synonyms")
        values = [str(value or "").strip() for value in synonyms] if isinstance(synonyms, list) else []
        terms.append({"name": word, "values": [value for value in values if value]})
    return terms


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _version_settings(bot: Bot, version: BotVersion) -> dict[str, Any]:
    data_json = _as_dict(bot.data_json)
    settings_by_version = _as_dict(data_json.get("settings_by_version"))
    version_id = str(getattr(version, "id", "") or "")
    version_name = str(getattr(version, "name", "") or "")
    return _as_dict(settings_by_version.get(version_id)) or _as_dict(settings_by_version.get(version_name))


def _webchat_botstation_connection(bot: Bot, version: BotVersion) -> dict[str, Any] | None:
    botstation = _as_dict(_version_settings(bot, version).get("botstation"))
    if not botstation:
        return {
            "channelCode": "WEBCHAT",
            "enabled": True,
            "botIdentifier": str(bot.id),
            "source": "legacy-default",
        }
    if botstation.get("connected") is not True or botstation.get("enabled") is not True:
        return None
    for item in _as_list(botstation.get("channels")):
        if not isinstance(item, dict):
            continue
        if str(item.get("channelCode") or "").upper() != "WEBCHAT":
            continue
        if item.get("enabled") is not True:
            return None
        identifier = str(item.get("botIdentifier") or "").strip()
        if identifier and identifier != str(bot.id):
            return None
        return item
    return None


def _ensure_webchat_connection(db, bot: Bot, version: BotVersion) -> None:
    admin_channel = db.scalar(
        select(AdminChannel).where(
            AdminChannel.organization_id == bot.organization_id,
            AdminChannel.code == "WEBCHAT",
            AdminChannel.status == "active",
            AdminChannel.deleted_at.is_(None),
        )
    )
    if admin_channel is None or _webchat_botstation_connection(bot, version) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="봇스테이션에 연결된 Webchat 운영봇만 실행할 수 있습니다.")


def _runtime_block_reason(bot: Bot, version: BotVersion) -> str | None:
    block_reason = runtime_block_reason(_version_ai_config(bot, version))
    if block_reason:
        return block_reason
    diagnostics = scenario_validation_from_version(version.version_json)
    if int(diagnostics.get("error_count") or 0) > 0:
        return scenario_validation_block_reason(diagnostics)
    return secondary_llm_block_reason(
        version.version_json, _version_ai_config(bot, version),
        bot_kind=str((bot.data_json or {}).get("bot_kind") or "bot"),
    )


def _ensure_runtime_supported(bot: Bot, version: BotVersion) -> None:
    reason = _runtime_block_reason(bot, version)
    if reason:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)


def _get_active_bot_version(bot_id: str) -> tuple[Bot, BotVersion, Group | None]:
    with SessionLocal() as db:
        bot = db.scalar(
            select(Bot).where(
                Bot.id == UUID(str(bot_id).strip()),
                Bot.status == "active",
                Bot.deleted_at.is_(None),
            )
        )
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webchat 봇을 찾을 수 없습니다.")
        if bot.active_version_id is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="운영버전이 지정되지 않은 봇입니다.")

        version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.bot_id == bot.id,
                BotVersion.status == "active",
                BotVersion.deleted_at.is_(None),
            )
        )
        if version is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="운영버전을 찾을 수 없습니다.")

        _ensure_webchat_connection(db, bot, version)
        _ensure_runtime_supported(bot, version)
        group = db.scalar(select(Group).where(Group.id == bot.group_id))
        return bot, version, group


def _list_active_webchat_bots(include_runtime_blocked: bool = False) -> list[tuple[Bot, BotVersion, Group | None]]:
    with SessionLocal() as db:
        bots = db.scalars(
            select(Bot).where(
                Bot.status == "active",
                Bot.active_version_id.is_not(None),
                Bot.deleted_at.is_(None),
            ).order_by(Bot.name.asc())
        ).all()
        items: list[tuple[Bot, BotVersion, Group | None]] = []
        for bot in bots:
            version = db.scalar(
                select(BotVersion).where(
                    BotVersion.id == bot.active_version_id,
                    BotVersion.bot_id == bot.id,
                    BotVersion.status == "active",
                    BotVersion.deleted_at.is_(None),
                )
            )
            if version is None:
                continue
            if _webchat_botstation_connection(bot, version) is None:
                continue
            if not include_runtime_blocked and _runtime_block_reason(bot, version):
                continue
            group = db.scalar(select(Group).where(Group.id == bot.group_id))
            items.append((bot, version, group))
        return items


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _compact_text(value: object) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣_]+", "", _normalize_text(value))


def _semantic_match_key(value: object) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", _normalize_text(value))


def _dictionary_canonical_map(dictionary_terms: list[dict[str, Any]]) -> dict[str, str]:
    canonical_map: dict[str, str] = {}
    for item in dictionary_terms:
        word_key = _semantic_match_key(item.get("word") or item.get("name"))
        if not word_key:
            continue
        canonical_map[word_key] = word_key
        synonyms = item.get("synonyms")
        if not isinstance(synonyms, list):
            synonyms = item.get("values")
        if isinstance(synonyms, list):
            for synonym in synonyms:
                synonym_key = _semantic_match_key(synonym)
                if synonym_key:
                    canonical_map[synonym_key] = word_key
    return canonical_map


def _apply_dictionary_canonical_key(value: object, canonical_map: dict[str, str]) -> str:
    key = _semantic_match_key(value)
    if not key or not canonical_map:
        return key
    for source, target in sorted(canonical_map.items(), key=lambda item: len(item[0]), reverse=True):
        if source and source != target and source in key:
            key = key.replace(source, target)
    return key


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^0-9a-zA-Z가-힣_]+", _normalize_text(value)) if token}


def _safe_dialogs(document: dict[str, Any]) -> list[dict[str, Any]]:
    dialogs = document.get("dialogs")
    return [item for item in dialogs if isinstance(item, dict)] if isinstance(dialogs, list) else []


def _dialog_utterances(dialog: dict[str, Any]) -> list[str]:
    utterances = dialog.get("utterances")
    if not isinstance(utterances, list):
        return []
    texts: list[str] = []
    for item in utterances:
        if isinstance(item, str) and item.strip():
            texts.append(item.strip())
        elif isinstance(item, dict) and str(item.get("text") or "").strip():
            texts.append(str(item.get("text") or "").strip())
    return texts


def _score_dialog(dialog: dict[str, Any], message: str) -> float:
    message_text = _normalize_text(message)
    message_tokens = _tokens(message_text)
    best_score = 0.0
    for utterance in _dialog_utterances(dialog):
        utterance_text = _normalize_text(utterance)
        if not utterance_text:
            continue
        if message_text == utterance_text:
            best_score = max(best_score, 1.0)
            continue
        if utterance_text in message_text or message_text in utterance_text:
            best_score = max(best_score, 0.85)
            continue
        utterance_tokens = _tokens(utterance_text)
        if message_tokens and utterance_tokens:
            best_score = max(best_score, len(message_tokens & utterance_tokens) / len(message_tokens | utterance_tokens))
    return best_score


def _exact_utterance_dialog(
    document: dict[str, Any],
    message: str,
    *,
    canonical_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    message_text = _apply_dictionary_canonical_key(message, canonical_map or {}) if canonical_map else _normalize_text(message)
    if not message_text:
        return None
    for dialog in _safe_dialogs(document):
        dialog_type = dialog.get("dialogType")
        if str("1" if dialog_type is None else dialog_type) not in {"1", "1.0"}:
            continue
        for utterance in _dialog_utterances(dialog):
            utterance_text = (
                _apply_dictionary_canonical_key(utterance, canonical_map or {}) if canonical_map else _normalize_text(utterance)
            )
            if utterance_text == message_text:
                return dialog
    return None


def _exact_intent_name_dialog(
    document: dict[str, Any],
    message: str,
    *,
    canonical_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    message_text = _apply_dictionary_canonical_key(message, canonical_map or {}) if canonical_map else _compact_text(message)
    if not message_text:
        return None
    for dialog in _safe_dialogs(document):
        dialog_type = dialog.get("dialogType")
        if str("1" if dialog_type is None else dialog_type) not in {"1", "1.0"}:
            continue
        candidates = [
            dialog.get("name"),
            dialog.get("displayName"),
            dialog.get("dialogKey"),
        ]
        if canonical_map:
            if any(_apply_dictionary_canonical_key(candidate, canonical_map) == message_text for candidate in candidates):
                return dialog
        elif any(_compact_text(candidate) == message_text for candidate in candidates):
            return dialog
    return None


def _exacting_matching_enabled(bot: Bot, version: BotVersion) -> bool:
    conversation_defaults = _as_dict(_version_settings(bot, version).get("conversationDefaults"))
    exacting_matching = _as_dict(conversation_defaults.get("exactingMatching"))
    return not (isinstance(exacting_matching, dict) and exacting_matching.get("enabled") is False)


def _select_dialog(document: dict[str, Any], message: str, *, prefer_exact_utterance: bool = True) -> tuple[dict[str, Any] | None, float]:
    if prefer_exact_utterance:
        exact_dialog = _exact_utterance_dialog(document, message)
        if exact_dialog is not None:
            return exact_dialog, 1.0
    candidates = [dialog for dialog in _safe_dialogs(document) if str(dialog.get("dialogType") or "1") in {"1", "1.0"}]
    scored = sorted(((dialog, _score_dialog(dialog, message)) for dialog in candidates), key=lambda item: item[1], reverse=True)
    if not scored or scored[0][1] <= 0:
        return None, 0.0
    return scored[0]


def _dialog_by_id_or_name(document: dict[str, Any], intent_id: str, intent_name: str = "") -> dict[str, Any] | None:
    for dialog in _safe_dialogs(document):
        if str(dialog.get("dialogType") or "1") not in {"1", "1.0"}:
            continue
        candidates = {
            str(dialog.get("id") or "").strip(),
            str(dialog.get("name") or "").strip(),
            str(dialog.get("displayName") or "").strip(),
            str(dialog.get("dialogKey") or "").strip(),
        }
        if intent_id in candidates or (intent_name and intent_name in candidates):
            return dialog
    return None


def _semantic_select_dialog(
    document: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    *,
    top_k: int = 3,
) -> tuple[dict[str, Any] | None, float]:
    dictionary_terms = _dictionary_terms_from_version(version.version_json)
    canonical_map = _dictionary_canonical_map(dictionary_terms)
    exact_intent_dialog = _exact_intent_name_dialog(document, message, canonical_map=canonical_map)
    if exact_intent_dialog is not None:
        return exact_intent_dialog, 1.0
    client = IntentVectorSearchClient(intent_vector_config(_version_ai_config(bot, version)))
    matches = client.search(
        bot_id=str(bot.id),
        version_id=str(version.id),
        query=message,
        top_k=top_k,
        dictionary_terms=dictionary_terms,
    )
    for match in matches:
        dialog = _dialog_by_id_or_name(document, match.intent_id, match.intent_name)
        if dialog is not None:
            return dialog, match.score
    return None, 0.0


def _llm_select_dialog(
    document: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    *,
    top_k: int = 3,
) -> tuple[dict[str, Any] | None, float]:
    exact_intent_dialog = _exact_intent_name_dialog(document, message)
    if exact_intent_dialog is not None:
        return exact_intent_dialog, 1.0
    result = classify_intent_with_llm_snapshot(
        training_snapshot=_nlu_training_state(version),
        ai_config=_version_ai_config(bot, version),
        query=message,
        top_k=top_k,
    )
    for candidate in result.candidates:
        dialog = _dialog_by_id_or_name(document, candidate.intent_id, candidate.intent_name)
        if dialog is not None:
            return dialog, candidate.confidence
    return None, 0.0


def _select_dialog_for_bot(
    document: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    *,
    prefer_exact_utterance: bool = True,
) -> tuple[dict[str, Any] | None, float]:
    data_json = _version_ai_config(bot, version)
    nlu_type = str(data_json.get("nlu_type") or data_json.get("nluType") or "ml")
    if nlu_type in {"semantic", "semantic_vector", "semantic_external"}:
        if prefer_exact_utterance:
            dictionary_terms = _dictionary_terms_from_version(version.version_json)
            exact_dialog = _exact_utterance_dialog(
                document,
                message,
                canonical_map=_dictionary_canonical_map(dictionary_terms),
            )
            if exact_dialog is not None:
                return exact_dialog, 1.0
        try:
            return _semantic_select_dialog(document, bot, version, message)
        except VectorSearchError as error:
            logger.warning(
                "Semantic vector intent search failed.",
                extra={
                    "event": "webchat.semantic_nlu.search_failed",
                    "extra_data": {"bot_id": str(bot.id), "version_id": str(version.id), "error": str(error)},
                },
            )
            return None, 0.0
    if prefer_exact_utterance:
        exact_dialog = _exact_utterance_dialog(document, message)
        if exact_dialog is not None:
            return exact_dialog, 1.0
    if nlu_type == "llm":
        try:
            return _llm_select_dialog(document, bot, version, message)
        except LlmClientError as error:
            logger.warning(
                "LLM intent classification failed.",
                extra={
                    "event": "webchat.llm_nlu.classification_failed",
                    "extra_data": {"bot_id": str(bot.id), "version_id": str(version.id), "error": str(error)},
                },
            )
            return None, 0.0
    return _select_dialog(document, message, prefer_exact_utterance=prefer_exact_utterance)


def _reply_for_dialog(dialog: dict[str, Any] | None, bot: Bot) -> str:
    if dialog is None:
        return resolve_default_message_text(bot.organization_id, "intent_fallback")
    for key in ("fallbackResponse", "response", "answer", "message"):
        value = dialog.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    name = str(dialog.get("displayName") or dialog.get("name") or "의도").strip()
    template = resolve_default_message_text(
        bot.organization_id,
        "intent_receipt",
        fallback="{{intentName}} 의도로 접수되었습니다.",
    )
    return template.replace("{{intentName}}", name)


def _serialize_bootstrap(bot: Bot, version: BotVersion, group: Group | None) -> dict[str, Any]:
    return {
        "bot": {
            "id": str(bot.id),
            "name": bot.name,
            "groupId": str(bot.group_id),
            "groupName": group.name if group else None,
            "activeVersionId": str(version.id),
            "activeVersionName": version.name,
            "activeVersionNo": version.version_no,
            "activatedAt": _iso(version.activated_at),
        },
        "participants": [
            {"id": "visitor", "kind": "user", "name": "사용자"},
            {"id": str(bot.id), "kind": "bot", "name": bot.name},
        ],
    }


@router.get("/bootstrap")
def bootstrap_webchat(
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    _verify_webchat_key(x_aidot_webchat_key)
    items = _list_active_webchat_bots(include_runtime_blocked=True)
    return success_response(
        request,
        {
            "bots": [_serialize_bootstrap(bot, version, group)["bot"] for bot, version, group in items],
            "participants": [
                {"id": "visitor", "kind": "user", "name": "사용자"},
                *[
                    {"id": str(bot.id), "kind": "bot", "name": bot.name, "botId": str(bot.id)}
                    for bot, _version, _group in items
                ],
            ],
        },
    )


def _webchat_secret_digest(secret: str) -> str:
    return hashlib.sha256(("cga-alone:webchat:v1:" + secret).encode("utf-8")).hexdigest()


def _webchat_credential_expiry(bot, version):
    timeout = _as_dict(_as_dict(_version_settings(bot, version).get("conversationDefaults")).get("timeout"))
    if timeout.get("enabled") is False:
        return None
    try:
        seconds = max(1, int(timeout.get("seconds", 120)))
    except (ValueError, TypeError, OverflowError):
        seconds = 120
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _credential_denied():
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채팅방을 찾을 수 없습니다.")


def _validate_webchat_credential(db, bot, client_room_id, token):
    from app.api.routes import channels
    try:
        if not isinstance(token, str) or len(token) > 256:
            raise ValueError
        raw_room_id, secret = token.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise ValueError
        room_id = UUID(raw_room_id)
    except (ValueError, TypeError, AttributeError):
        raise _credential_denied() from None
    room = db.get(ChannelRoom, room_id)
    if room is None or room.deleted_at is not None or room.status != "open" or room.channel_type != "webchat" or room.bot_id != bot.id or channels._is_persistent_room(db, room_id):
        raise _credential_denied()
    binding = _as_dict(_as_dict(room.metadata_json).get("__webchatAccess"))
    digest = binding.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise _credential_denied()
    if (binding.get("bot_id") != str(bot.id) or binding.get("organization_id") != str(bot.organization_id)
        or binding.get("channel") != "webchat" or binding.get("actor_id") != room.participant_id
        or binding.get("client_room_hash") != hashlib.sha256(client_room_id.encode("utf-8")).hexdigest()
        or not hmac.compare_digest(digest, _webchat_secret_digest(secret))):
        raise _credential_denied()
    expires = binding.get("expires_at")
    if expires is not None:
        try:
            if datetime.fromisoformat(expires) <= datetime.now(timezone.utc):
                raise ValueError
        except (ValueError, TypeError):
            raise _credential_denied() from None
    return room.id, dict(binding)


def _create_native_webchat_message(bot, version, room_id, payload, request, api_key, credential):
    """주소·참여자 입력은 표시 식별자이고, 저장 방 재사용은 서버 자격 증명으로만 허용한다."""
    from app.api.routes import channels
    issued_token = None
    if credential is not None:
        with SessionLocal() as db:
            native_room_id, binding = _validate_webchat_credential(db, bot, room_id, credential)
    else:
        actor_id = "wc:" + uuid4().hex
        created = channels.create_channel_room(
            "webchat", channels.ChannelRoomCreateRequest(bot_id=str(bot.id), client_room_id="wc-auth:" + uuid4().hex,
                participant_id=actor_id, start_immediately=False), request, api_key,
        )["data"]
        native_room_id = UUID(created["room"]["id"])
        secret = secrets.token_urlsafe(32)
        binding = {"digest": _webchat_secret_digest(secret), "actor_id": actor_id, "bot_id": str(bot.id),
            "organization_id": str(bot.organization_id), "channel": "webchat",
            "client_room_hash": hashlib.sha256(room_id.encode("utf-8")).hexdigest(),
            "expires_at": _webchat_credential_expiry(bot, version)}
        with SessionLocal() as db:
            room = db.get(ChannelRoom, native_room_id)
            room.metadata_json = {**_as_dict(room.metadata_json), "__webchatAccess": binding}
            flag_modified(room, "metadata_json")
            db.commit()
        issued_token = str(native_room_id) + "." + secret
    with channels._webchat_room_access(native_room_id):
        result = channels.create_channel_room_message(
            "webchat", native_room_id, channels.ChannelMessageRequest(message=payload.message,
                participant_id=binding["actor_id"]), request, api_key,
        )["data"]
    with SessionLocal() as db:
        room = db.get(ChannelRoom, native_room_id)
        if room is not None:
            # 일반 운영 버전 전환과 종료에서도 보호 marker는 사라지면 안 된다.
            binding["expires_at"] = _webchat_credential_expiry(bot, version)
            room.metadata_json = {**_as_dict(room.metadata_json), "__webchatAccess": binding}
            flag_modified(room, "metadata_json")
            db.commit()
    messages = result.get("botMessages") or []
    user_message = {**_as_dict(result.get("userMessage")), "participantId": payload.participant_id or "visitor"}
    return success_response(request, {
        "roomId": room_id,
        "bot": {"id": str(bot.id), "name": bot.name},
        "activeVersion": {"id": str(version.id), "name": version.name, "versionNo": version.version_no},
        "userMessage": user_message,
        "botMessage": messages[-1] if messages else None,
        "botMessages": messages,
        "intent": result.get("intent"),
        **({"conversationToken": issued_token} if issued_token else {}),
    })


@contextmanager
def _shared_processing_lease(room_id, actor, generation, context):
    from app.services.room_turn import renew_room_turn_processing_lease
    stop = Event()
    def renew():
        while not stop.wait(10):
            try:
                with SessionLocal() as db:
                    # 갱신 transaction은 짧게 끝내고 외부 응답을 기다리는 동안 유지하지 않는다.
                    from sqlalchemy import text
                    db.execute(text("SET LOCAL lock_timeout = '2s'"))
                    if not renew_room_turn_processing_lease(db, room_id, actor, generation, lease_seconds=30):
                        context["lease_lost"] = True
                        return
                    db.commit()
            except Exception:
                context["lease_lost"] = True
                return
    worker = Thread(target=renew, name="cga-shared-turn-lease", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=max(5, int(settings.db_connect_timeout_seconds) + 3))


def _shared_runtime_message(bot, version, room_id, payload, request, api_key, credential):
    from app.api.routes import channels
    from app.services.room_turn import claim_room_turn, renew_room_turn_processing_lease
    try:
        native_id = UUID(room_id)
    except (ValueError, TypeError):
        raise _credential_denied() from None
    with SessionLocal() as db:
        room, _binding = _shared_room(db, bot, native_id, lock=True)
        room, member = _shared_member(db, bot, native_id, credential)
        actor = member["actor_id"]
        if payload.addressed is not True:
            token = channels._shared_turn_capability.set(None)
            try:
                message = channels._add_message(db, room, actor, "user", member.get("participant_name") or "사용자", payload.message)
            finally:
                channels._shared_turn_capability.reset(token)
            db.commit();db.refresh(message)
            return success_response(request, {"roomId": room_id, "userMessage": channels._serialize_message(message),
                "botMessage": None, "botMessages": [], "turn": _shared_turn_view(db, room, actor)})
        previous_generation = _as_dict(_as_dict(room.metadata_json).get("__roomTurn")).get("generation")
        version_replaced = room.bot_version_id != version.id
        if version_replaced:
            previous_version_id = channels._apply_active_version_to_room(room, bot, version, "webchat")
            channels._write_channel_audit_log(db, request, "channel.session.version_applied", room.id,
                {"channel": "webchat", "bot_id": str(bot.id), "previous_version_id": previous_version_id, "active_version_id": str(version.id)})
            db.flush()
        decision = claim_room_turn(db, room, actor, True)
        if not decision.allowed:
            raise HTTPException(status_code=409, detail="이 방의 다른 질문이 진행 중입니다.")
        if decision.reason in {"expired", "version_changed"} or version_replaced:
            previous = _as_dict(room.metadata_json)
            fresh = channels._initial_runtime_state_for_version(bot, version, "webchat")
            for key in ("__webchatAccess", "__roomTurn", "conversationHistory", "language"):
                if key in previous:
                    fresh[key] = previous[key]
            room.metadata_json = fresh
            flag_modified(room, "metadata_json")
            if previous_generation:
                abandoned = db.scalars(select(ChannelQueueEvent).where(ChannelQueueEvent.room_id == room.id,
                    ChannelQueueEvent.status == "processing",
                    ChannelQueueEvent.parameter_json["sharedTurnGeneration"].astext == previous_generation)).all()
                for event in abandoned:
                    event.status = "unknown";event.receive_status = "failed"
                    event.status_changed_at = datetime.now(timezone.utc)
                    event.result_json = {"outcome": "unknown", "reason": "shared_turn_expired"}
            db.flush()
        if not renew_room_turn_processing_lease(db, room.id, actor, decision.generation, lease_seconds=30):
            raise HTTPException(status_code=409, detail="질문 차례를 확보하지 못했습니다.")
        db.commit()
    with channels._webchat_room_access(native_id), channels._shared_room_turn_access(native_id, actor, decision.generation, member.get("participant_name") or "사용자") as context:
        try:
            with _shared_processing_lease(native_id, actor, decision.generation, context):
                result = channels.create_channel_room_message("webchat", native_id,
                    channels.ChannelMessageRequest(message=payload.message, participant_id=actor), request, api_key)["data"]
        except Exception:
            # 결과가 불명확한 이전 API를 자동 재실행하지 않는다.
            if context["queue_event_id"] is not None:
                with SessionLocal() as db:
                    event = db.get(ChannelQueueEvent, context["queue_event_id"])
                    if event is not None and event.status == "processing":
                        event.status = "unknown";event.receive_status = "failed"
                        event.result_json = {"outcome": "unknown", "reason": "shared_turn_interrupted"}
                        db.commit()
            raise
    with SessionLocal() as db:
        room = db.scalar(select(ChannelRoom).where(ChannelRoom.id == native_id).with_for_update().execution_options(populate_existing=True))
        access = _as_dict(_as_dict(room.metadata_json).get("__webchatAccess"))
        members = dict(_as_dict(access.get("members")))
        if actor in members:
            members[actor] = {**_as_dict(members[actor]), "expires_at": _webchat_credential_expiry(bot, version)}
            room.metadata_json = {**_as_dict(room.metadata_json), "__webchatAccess": {**access, "members": members}}
            flag_modified(room, "metadata_json");db.commit()
        result["turn"] = _shared_turn_view(db, room, actor)
    return success_response(request, result)


def _shared_target(bot_id, api_key):
    _verify_webchat_key(api_key)
    bot, version, _group = _get_active_bot_version(bot_id)
    if not isinstance(_version_ai_config(bot, version).get("secondary_llm"), dict) or _as_dict(bot.data_json).get("bot_kind") == "hub":
        raise HTTPException(status_code=409, detail="공유방은 2차 답변 모듈이 설정된 운영봇에서 사용합니다.")
    return bot, version


def _shared_room(db, bot, room_id, *, lock=False):
    from app.api.routes import channels
    query = select(ChannelRoom).where(ChannelRoom.id == room_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    room = db.scalar(query)
    if (room is None or room.deleted_at is not None or room.status != "open" or room.channel_type != "webchat"
        or room.bot_id != bot.id or channels._is_persistent_room(db, room_id)):
        raise _credential_denied()
    binding = _as_dict(_as_dict(room.metadata_json).get("__webchatAccess"))
    if (binding.get("mode") != "shared" or binding.get("bot_id") != str(bot.id)
        or binding.get("organization_id") != str(bot.organization_id) or binding.get("channel") != "webchat"
        or binding.get("client_room_hash") != hashlib.sha256(str(room.id).encode("utf-8")).hexdigest()
        or not isinstance(binding.get("members"), dict)):
        raise _credential_denied()
    return room, binding


def _shared_member(db, bot, room_id, token):
    room, binding = _shared_room(db, bot, room_id)
    try:
        if not isinstance(token, str) or len(token) > 256:
            raise ValueError
        token_room, secret = token.split(".", 1)
        if UUID(token_room) != room.id or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _credential_denied() from None
    matched = None
    digest = _webchat_secret_digest(secret)
    for actor, value in binding["members"].items():
        member = _as_dict(value)
        stored = member.get("digest")
        if (isinstance(actor, str) and re.fullmatch(r"wc:[0-9a-f]{32}", actor)
            and isinstance(stored, str) and re.fullmatch(r"[0-9a-f]{64}", stored)
            and hmac.compare_digest(stored, digest)):
            matched = {**member, "actor_id": actor}
    if matched is None:
        raise _credential_denied()
    expires = matched.get("expires_at")
    if expires is not None:
        try:
            deadline = datetime.fromisoformat(expires)
            if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
                raise ValueError
        except (ValueError, TypeError):
            raise _credential_denied() from None
    return room, matched


def _shared_turn_view(db, room, actor):
    from app.services import room_turn
    turn = _as_dict(room.metadata_json).get("__roomTurn")
    idle = {"state": "idle", "ownerParticipantId": None, "canAskBot": True}
    if turn is None:
        return idle
    if not room_turn._valid_turn(turn):
        return {"state": "processing", "ownerParticipantId": None, "canAskBot": False}
    try:
        if room_turn._expired(db, turn, "waiting_expires_at" if turn["phase"] == "waiting_input" else "processing_expires_at"):
            return idle
    except (ValueError, TypeError):
        return {"state": "processing", "ownerParticipantId": None, "canAskBot": False}
    waiting = turn["phase"] == "waiting_input"
    return {"state": "waiting_input" if waiting else "processing", "ownerParticipantId": turn["actor_id"],
        "canAskBot": waiting and turn["actor_id"] == actor}


def _issue_shared_member(db, room, binding, bot, version, name):
    actor, secret = "wc:" + uuid4().hex, secrets.token_urlsafe(32)
    members = {**binding["members"], actor: {"digest": _webchat_secret_digest(secret),
        "participant_name": name, "expires_at": _webchat_credential_expiry(bot, version)}}
    room.metadata_json = {**_as_dict(room.metadata_json), "__webchatAccess": {**binding, "members": members}}
    flag_modified(room, "metadata_json")
    db.commit()
    return {"roomId": str(room.id), "participantId": actor, "conversationToken": str(room.id) + "." + secret,
        "turn": _shared_turn_view(db, room, actor)}


@router.post("/bots/{bot_id}/shared-rooms")
def create_shared_webchat_room(bot_id: str, payload: WebchatSharedParticipantRequest, request: Request,
    x_aidot_webchat_key: str | None = Header(default=None)):
    from app.api.routes import channels
    bot, version = _shared_target(bot_id, x_aidot_webchat_key)
    created = channels.create_channel_room("webchat", channels.ChannelRoomCreateRequest(bot_id=str(bot.id),
        client_room_id="wc-auth:" + uuid4().hex, participant_id="wc:" + uuid4().hex,
        participant_name=payload.participant_name, start_immediately=False), request, x_aidot_webchat_key)["data"]
    with SessionLocal() as db:
        room = db.get(ChannelRoom, UUID(created["room"]["id"]))
        binding = {"mode": "shared", "bot_id": str(bot.id), "organization_id": str(bot.organization_id),
            "channel": "webchat", "client_room_hash": hashlib.sha256(str(room.id).encode("utf-8")).hexdigest(), "members": {}}
        return success_response(request, _issue_shared_member(db, room, binding, bot, version, payload.participant_name))


@router.post("/bots/{bot_id}/shared-rooms/{room_id}/participants")
def join_shared_webchat_room(bot_id: str, room_id: UUID, payload: WebchatSharedParticipantRequest, request: Request,
    x_aidot_webchat_key: str | None = Header(default=None)):
    bot, version = _shared_target(bot_id, x_aidot_webchat_key)
    with SessionLocal() as db:
        room, binding = _shared_room(db, bot, room_id, lock=True)
        return success_response(request, _issue_shared_member(db, room, binding, bot, version, payload.participant_name))


@router.get("/bots/{bot_id}/shared-rooms/{room_id}/messages")
def get_shared_webchat_messages(bot_id: str, room_id: UUID, request: Request,
    x_aidot_webchat_key: str | None = Header(default=None), x_aidot_conversation_token: str | None = Header(default=None)):
    from app.api.routes import channels
    bot, _version = _shared_target(bot_id, x_aidot_webchat_key)
    with SessionLocal() as db:
        room, member = _shared_member(db, bot, room_id, x_aidot_conversation_token)
        messages = db.scalars(select(ChannelMessage).where(ChannelMessage.room_id == room.id).order_by(ChannelMessage.created_at, ChannelMessage.id)).all()
        return success_response(request, {"roomId": str(room.id), "messages": [channels._serialize_message(item) for item in messages],
            "turn": _shared_turn_view(db, room, member["actor_id"])})


@router.post("/bots/{bot_id}/rooms/{room_id}/messages")
def create_webchat_room_message(
    bot_id: str,
    room_id: str,
    payload: WebchatMessageRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
    x_aidot_conversation_token: str | None = Header(default=None),
) -> dict[str, object]:
    _verify_webchat_key(x_aidot_webchat_key)
    bot, version, _group = _get_active_bot_version(bot_id)
    credential = x_aidot_conversation_token if isinstance(x_aidot_conversation_token, str) else None
    secondary_enabled = isinstance(_version_ai_config(bot, version).get("secondary_llm"), dict) and _as_dict(bot.data_json).get("bot_kind") != "hub"
    if credential is not None and not secondary_enabled:
        raise _credential_denied()
    if secondary_enabled and credential is not None:
        try:
            candidate_id = UUID(credential.split(".", 1)[0])
        except (ValueError, TypeError, AttributeError):
            raise _credential_denied() from None
        with SessionLocal() as db:
            candidate = db.get(ChannelRoom, candidate_id)
            shared = _as_dict(_as_dict(candidate.metadata_json if candidate else None).get("__webchatAccess")).get("mode") == "shared"
        if shared:
            return _shared_runtime_message(bot, version, room_id, payload, request, x_aidot_webchat_key, credential)
    if secondary_enabled:
        return _create_native_webchat_message(bot, version, room_id, payload, request, x_aidot_webchat_key, credential)
    document = normalize_version_document(version.version_json)
    selected_dialog, score = _select_dialog_for_bot(
        document,
        bot,
        version,
        payload.message,
        prefer_exact_utterance=_exacting_matching_enabled(bot, version),
    )
    now = datetime.now(timezone.utc).isoformat()

    return success_response(
        request,
        {
            "roomId": room_id,
            "bot": {"id": str(bot.id), "name": bot.name},
            "activeVersion": {"id": str(version.id), "name": version.name, "versionNo": version.version_no},
            "userMessage": {
                "participantId": payload.participant_id or "visitor",
                "text": payload.message,
                "createdAt": now,
            },
            "botMessage": {
                "participantId": str(bot.id),
                "participantKind": "bot",
                "text": _reply_for_dialog(selected_dialog, bot),
                "createdAt": now,
            },
            "intent": {
                "id": str(selected_dialog.get("id")) if selected_dialog else None,
                "name": str(selected_dialog.get("name")) if selected_dialog else None,
                "score": round(score * 100, 2),
            },
        },
    )
