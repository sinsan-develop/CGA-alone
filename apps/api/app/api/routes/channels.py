from __future__ import annotations

from app.services.secondary_llm import SecondaryOutcome, run_secondary_module, resume_secondary_outcome, capture_secondary_terminal_result

import json
from contextlib import contextmanager
from contextvars import ContextVar
from io import BytesIO
from copy import deepcopy
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import ROOT_DIR, settings
from app.core.language import language_candidates, normalize_supported_language
from app.core.logging import get_logger
from app.core.responses import success_response
from app.core.version_documents import normalize_version_document
from app.db.session import SessionLocal
from app.models import AdminChannel, AuditLog, Bot, BotHub, BotVersion, ChannelMessage, ChannelQueueEvent, ChannelRoom, Group
from app.services.hub_runtime import (
    active_hub_members,
    hub_configuration,
    hub_selection_output,
    is_hub_bot,
    resolve_hub_member,
)
from app.services.default_messages import DEFAULT_MESSAGE_FALLBACKS, get_default_message_text
from app.services.bot_ai_policy import runtime_block_reason, secondary_llm_block_reason
from app.services.llm_client import LlmChatClient, LlmClientError, resolve_llm_provider_config
from app.services.llm_nlu import classify_intent_with_llm_snapshot
from app.services.nlu.deep_learning_lite import classify_deep_learning_lite_model, score_deep_learning_lite_model
from app.services.runtime_session import apply_end_card_state, runtime_completion_reason
from app.services.runtime_variables import evaluate_expression, get_variable, object_path_value, render_text, set_variable, stringify_variable, var_key
from app.services.scenario_validation import scenario_validation_block_reason, scenario_validation_from_version
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.vector_search import (
    AnswerVectorSearchClient,
    IntentVectorSearchClient,
    VectorSearchError,
    answer_vector_config,
    intent_vector_config,
)


router = APIRouter(prefix="/channels", tags=["channels"])
logger = get_logger("aidot.channels")
SUPPORTED_CHANNELS = {"webchat", "kakao", "ms-teams"}
NON_CHANNEL_QUEUE_TYPES = {"training"}
DEFAULT_LLM_ANSWER_SYSTEM_PROMPT = "사용자 질문에 답변한다."
LLM_RAG_ANSWER_SAFETY_PROMPT = (
    "반드시 제공된 rag_context 안의 내용만 근거로 답변한다. "
    "rag_context에 없는 사실, 추측, 일반지식, 외부지식은 절대 추가하지 않는다. "
    "근거가 부족하면 '제공된 문서에서 답변 근거를 찾을 수 없습니다.'라고 답한다."
)
LLM_JSON_ANSWER_FORMAT_PROMPT = "응답은 JSON 객체 하나만 반환한다. 형식: {\"answer\":\"...\"}"


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


class ChannelConnectRequest(BaseModel):
    client_id: str | None = Field(default=None, max_length=120)


class ChannelRoomCreateRequest(BaseModel):
    bot_id: str = Field(min_length=1, max_length=150)
    client_room_id: str | None = Field(default=None, max_length=150)
    participant_id: str | None = Field(default="visitor", max_length=120)
    participant_name: str | None = Field(default="사용자", max_length=120)
    use_configured_initial_messages: bool = Field(default=False)
    start_immediately: bool = Field(default=True)
    language: str | None = Field(default=None, max_length=35)


class ChannelMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    participant_id: str | None = Field(default="visitor", max_length=120)
    source_talk_node_id: str | None = Field(default=None, max_length=120)
    defer_processing: bool = False
    target_dialog_id: str | None = Field(default=None, max_length=150)
    dialog_params: dict[str, Any] = Field(default_factory=dict)
    system_name: str | None = Field(default=None, max_length=150)
    direct_dialog_root: bool = False
    language: str | None = Field(default=None, max_length=35)


class KakaoWebhookRequest(BaseModel):
    userRequest: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    bot: dict[str, Any] = Field(default_factory=dict)
    contexts: list[dict[str, Any]] = Field(default_factory=list)


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _message_language_candidates(
    payload_language: object,
    accept_language: object,
    bot: Any,
    room: ChannelRoom | None = None,
) -> tuple[str, ...]:
    bot_language = (getattr(bot, "data_json", None) or {}).get("language")
    has_request_language = normalize_supported_language(payload_language) is not None
    has_header_language = isinstance(accept_language, str) and any(
        normalize_supported_language(item.split(";", 1)[0]) is not None
        for item in accept_language.split(",")
    )
    if has_request_language or has_header_language:
        return language_candidates(payload_language, accept_language, bot_language)
    room_language = (_as_dict(room.metadata_json).get("language") if room is not None else None)
    return language_candidates(room_language, None, bot_language)


def _normalize_kakao_webhook_request(
    payload: dict[str, Any],
    *,
    bot_id_override: str | None = None,
) -> dict[str, Any]:
    user_request = _as_dict(payload.get("userRequest"))
    action = _as_dict(payload.get("action"))
    bot = _as_dict(payload.get("bot"))
    user = _as_dict(user_request.get("user"))
    user_properties = _as_dict(user.get("properties"))
    action_params = _as_dict(action.get("params"))
    action_client_extra = _as_dict(action.get("clientExtra"))
    user_params = _as_dict(user_request.get("params"))

    bot_id = _first_non_empty_text(
        bot_id_override,
        action_client_extra.get("botId"),
        action_params.get("botId"),
        user_params.get("botId"),
    )
    if not bot_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="카카오 webhook bot_id가 없습니다.")

    channel_user_id = _first_non_empty_text(
        user.get("id"),
        user_properties.get("plusfriendUserKey"),
        user_properties.get("appUserId"),
        user_properties.get("botUserKey"),
    )
    if not channel_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="카카오 사용자 식별값이 없습니다.")

    utterance = _first_non_empty_text(
        user_request.get("utterance"),
        user_request.get("value"),
        action_params.get("utterance"),
    )
    conversation_id = _first_non_empty_text(
        user_request.get("callbackUrl"),
        action.get("id"),
        user_request.get("timezone"),
    )
    channel_room_id = f"kakao:{bot_id}:{conversation_id or channel_user_id}"
    return {
        "channel": "kakao",
        "bot_id": bot_id,
        "channel_user_id": channel_user_id,
        "channel_room_id": channel_room_id,
        "utterance": utterance,
        "raw_payload": payload,
    }


def _channel_request_token(admin_channel: AdminChannel | None, connection: dict[str, Any] | None = None) -> str:
    admin_data = _as_dict(admin_channel.data_json if admin_channel is not None else None)
    auth_config = _as_dict(admin_data.get("auth_config"))
    configured = _first_non_empty_text(
        auth_config.get("token"),
        auth_config.get("channelToken"),
        auth_config.get("accessToken"),
        auth_config.get("secret"),
        auth_config.get("appSecret"),
    )
    if configured:
        return configured
    connection_data = _as_dict(connection)
    return _first_non_empty_text(
        connection_data.get("appSecret"),
        connection_data.get("channelToken"),
        connection_data.get("token"),
        connection_data.get("secret"),
    ) or ""


def _verify_kakao_channel_request(bot_id: str, presented_token: str | None) -> None:
    with SessionLocal() as db:
        bot, version, _group = _get_active_bot_version(db, bot_id, "kakao")
        connection = _ensure_botstation_connection(db, bot, version, "kakao")
        admin_channel = _get_active_admin_channel(db, bot.organization_id, "kakao")
        expected_token = _channel_request_token(admin_channel, connection)
        if not expected_token:
            return
        if not presented_token or presented_token.strip() != expected_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kakao 채널 인증에 실패했습니다.")


def _kakao_quick_reply(option: dict[str, str] | str) -> dict[str, str]:
    if isinstance(option, dict):
        label = str(option.get("label") or "").strip()
        action = str(option.get("action") or "message").strip() or "message"
        if action == "webLink":
            web_link_url = str(option.get("webLinkUrl") or "").strip()
            return {
                "label": label or web_link_url or "링크 열기",
                "action": "webLink",
                "webLinkUrl": web_link_url,
            }
        message_text = str(option.get("messageText") or label).strip()
        return {
            "label": label or message_text or "선택",
            "action": "message",
            "messageText": message_text or label or "선택",
        }
    normalized = str(option).strip()
    return {
        "label": normalized,
        "action": "message",
        "messageText": normalized,
    }


def _is_public_absolute_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith("https://") or normalized.startswith("http://")


def _kakao_local_image_path(value: str) -> Path | None:
    match = re.fullmatch(r"/files/(temp|bot-images)/([^?#]+)", value)
    if match is None:
        return None
    storage_dir = (ROOT_DIR / "storage" / match.group(1)).resolve()
    image_path = (storage_dir / match.group(2)).resolve()
    if not image_path.is_relative_to(storage_dir) or not image_path.is_file():
        return None
    return image_path


def _kakao_public_image_url(value: Any) -> str:
    image_url = str(value or "").strip()
    if not image_url:
        return ""
    if _is_public_absolute_url(image_url):
        return image_url
    image_path = _kakao_local_image_path(image_url)
    if image_path is None:
        return ""
    public_api_base_url = settings.next_public_api_base_url.strip().rstrip("/")
    if not public_api_base_url:
        return ""
    cache_version = f"contain-v1-{image_path.stat().st_mtime_ns}"
    return f"{public_api_base_url}/api/v1/channels/kakao/images?{urlencode({'path': image_url, 'v': cache_version})}"


@router.get("/kakao/images", include_in_schema=False)
def get_kakao_image(path: str = Query(..., min_length=1)) -> Response:
    image_path = _kakao_local_image_path(path)
    if image_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="카카오 이미지를 찾을 수 없습니다.")
    try:
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode != "RGB":
                background = Image.new("RGB", image.size, "white")
                if image.mode == "RGBA":
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            contained = ImageOps.contain(image, (800, 400), method=Image.Resampling.LANCZOS)
            normalized = Image.new("RGB", (800, 400), "white")
            normalized.paste(contained, ((800 - contained.width) // 2, (400 - contained.height) // 2))
            content = BytesIO()
            normalized.save(content, format="JPEG", quality=90, optimize=True)
    except (OSError, UnidentifiedImageError) as error:
        logger.warning("Kakao carousel image normalization failed.", extra={"path": str(image_path), "error": str(error)})
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="카카오 이미지 형식을 변환할 수 없습니다.") from error
    return Response(content.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

def _rich_form_components(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nested = value.get("richForm")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [value]
    return []


def _walk_rich_form_components(value: Any) -> list[dict[str, Any]]:
    components = _rich_form_components(value)
    walked: list[dict[str, Any]] = []
    stack = list(components)
    while stack:
        component = stack.pop(0)
        walked.append(component)
        for key in ("content", "contents", "items", "children", "tab", "tabs"):
            for child in _as_list(component.get(key)):
                if isinstance(child, dict):
                    stack.append(child)
    return walked


def _kakao_action_item(
    *,
    label: Any = "",
    message_text: Any = "",
    url: Any = "",
) -> dict[str, str] | None:
    normalized_label = str(label or "").strip()
    normalized_message = str(message_text or "").strip()
    normalized_url = str(url or "").strip()
    if _is_public_absolute_url(normalized_url):
        return {
            "label": normalized_label or normalized_url or "링크 열기",
            "action": "webLink",
            "webLinkUrl": normalized_url,
        }
    if normalized_message or normalized_label:
        value = normalized_message or normalized_label
        return {
            "label": normalized_label or value,
            "action": "message",
            "messageText": value,
        }
    return None


def _kakao_action_signature(action: dict[str, str]) -> str:
    action_type = str(action.get("action") or "").strip()
    if action_type == "webLink":
        return f"webLink::{str(action.get('label') or '').strip()}::{str(action.get('webLinkUrl') or '').strip()}"
    return f"message::{str(action.get('label') or '').strip()}::{str(action.get('messageText') or '').strip()}"


def _dedupe_kakao_actions(actions: list[dict[str, str]], limit: int | None = None) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for action in actions:
        signature = _kakao_action_signature(action)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(action)
        if limit is not None and len(deduped) >= limit:
            break
    return deduped


def _message_actions_from_options(value: Any) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            action = _kakao_action_item(
                label=item.get("label") or item.get("title") or item.get("text") or item.get("name") or item.get("value"),
                message_text=item.get("messageText") or item.get("message") or item.get("text") or item.get("value"),
                url=item.get("webLinkUrl") or item.get("url") or item.get("link") or item.get("href"),
            )
        else:
            action = _kakao_action_item(label=item, message_text=item)
        if action is not None:
            actions.append(action)
    return actions


def _rich_form_button_actions(value: Any) -> list[dict[str, str]]:
    parsed = _parse_rich_form_or_none(value)
    components = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
    actions: list[dict[str, str]] = []
    stack = [item for item in components if isinstance(item, dict)]
    while stack:
        component = stack.pop(0)
        component_type = str(component.get("type") or "").upper()
        if component_type.startswith("BUTTON"):
            for item in _as_list(component.get("button")) + _as_list(component.get("buttons")):
                if not isinstance(item, dict):
                    continue
                action = _kakao_action_item(
                    label=item.get("title") or item.get("label") or item.get("text") or item.get("value"),
                    message_text=item.get("value") or item.get("messageText") or item.get("title") or item.get("label") or item.get("text"),
                    url=item.get("url") or item.get("link") or item.get("webLinkUrl"),
                )
                if action is not None:
                    actions.append(action)
        for key in ("content", "contents", "items", "children", "tab", "tabs"):
            for child in _as_list(component.get(key)):
                if isinstance(child, dict):
                    stack.append(child)
    return actions


def _kakao_fallback_reasons(messages: list[dict[str, Any]], output_types: list[str]) -> list[str]:
    if "basicCard" in output_types or "carousel" in output_types or "listCard" in output_types:
        return []
    reasons: list[str] = []
    seen: set[str] = set()
    supported_rich_form_types = {"FORM_TITLE", "TEXT", "HR", "IMAGE", "BUTTON"}
    for message in messages:
        payload = _as_dict(message.get("payload"))
        card_payload = _as_dict(payload.get("card"))
        carousel_payload = _as_dict(payload.get("carousel"))
        image_url = str(card_payload.get("imageUrl") or "").strip()
        if image_url and not _is_public_absolute_url(image_url):
            reason = "card_non_public_image_url"
            if reason not in seen:
                seen.add(reason)
                reasons.append(reason)
        carousel_image_url = str(carousel_payload.get("imageUrl") or "").strip()
        if carousel_image_url and not _is_public_absolute_url(carousel_image_url):
            reason = "carousel_non_public_image_url"
            if reason not in seen:
                seen.add(reason)
                reasons.append(reason)
        for component in _walk_rich_form_components(payload.get("richForm")):
            component_type = str(component.get("type") or "").strip().upper()
            if not component_type:
                continue
            if component_type == "IMAGE":
                rich_image_url = str(component.get("url") or "").strip()
                if rich_image_url and not _is_public_absolute_url(rich_image_url):
                    reason = "rich_form_non_public_image_url"
                    if reason not in seen:
                        seen.add(reason)
                        reasons.append(reason)
                continue
            if component_type not in supported_rich_form_types:
                reason = f"unsupported_rich_form_component:{component_type}"
                if reason not in seen:
                    seen.add(reason)
                    reasons.append(reason)
    return reasons


def _kakao_list_card_response_from_serialized_messages(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in messages:
        payload = _as_dict(message.get("payload"))
        table_payload = _as_dict(payload.get("table"))
        rows = [row for row in _as_list(payload.get("rows")) if isinstance(row, dict)]
        if not rows:
            table_payload = _as_dict(payload.get("table"))
            rows = [row for row in _as_list(table_payload.get("rows")) if isinstance(row, dict)]
        if not rows:
            continue
        columns = [str(item or "").strip() for item in _as_list(payload.get("columns"))]
        if not any(columns):
            table_payload = _as_dict(payload.get("table"))
            columns = [str(item or "").strip() for item in _as_list(table_payload.get("columns"))]
        columns = [column for column in columns if column]
        if not columns and rows:
            columns = [str(key).strip() for key in rows[0].keys() if str(key).strip()]
        selectable = bool(payload.get("selectable") or table_payload.get("selectable"))
        key_column = str(payload.get("keyColumn") or table_payload.get("keyColumn") or (columns[0] if selectable and columns else "")).strip()

        items: list[dict[str, Any]] = []
        for row in rows[:5]:
            ordered_values = [str(row.get(column) or "").strip() for column in columns if str(row.get(column) or "").strip()]
            if not ordered_values:
                ordered_values = [str(value or "").strip() for value in row.values() if str(value or "").strip()]
            if not ordered_values:
                continue
            image_url = next((value for value in ordered_values if _is_public_absolute_url(value)), "")
            text_values = [value for value in ordered_values if value and value != image_url]
            selection_value = str(row.get(key_column) or "").strip() if key_column else ""
            display_values = list(text_values)
            if selectable and selection_value and selection_value in display_values:
                display_values.remove(selection_value)
            title = display_values[0] if display_values else (selection_value or image_url or f"항목 {len(items) + 1}")
            description = " / ".join(display_values[1:]) if len(display_values) > 1 else ""
            item: dict[str, Any] = {
                "title": title,
            }
            if description and description != title:
                item["description"] = description
            if image_url:
                item["imageUrl"] = image_url
            if selectable:
                item["action"] = "message"
                item["messageText"] = selection_value or title
            items.append(item)

        if not items:
            continue

        return {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "listCard": {
                            "header": {
                                "title": str(message.get("text") or "").strip() or "목록 안내",
                            },
                            "items": items,
                        }
                    }
                ]
            },
        }
    return None


def _kakao_basic_card_from_serialized_messages(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in messages:
        payload = _as_dict(message.get("payload"))
        card_payload = _as_dict(payload.get("card"))
        if card_payload:
            card_title = str(card_payload.get("title") or "").strip() or str(message.get("text") or "").strip() or "안내"
            card_description = str(card_payload.get("description") or "").strip()
            image_url = str(card_payload.get("imageUrl") or "").strip()
            if image_url and not _is_public_absolute_url(image_url):
                continue
            basic_card: dict[str, Any] = {
                "title": card_title,
            }
            if card_description and card_description != card_title:
                basic_card["description"] = card_description
            if image_url:
                basic_card["thumbnail"] = {"imageUrl": image_url, "fixedRatio": True}
            card_actions = _dedupe_kakao_actions(_message_actions_from_options(payload.get("options")), limit=3)
            if card_actions:
                basic_card["buttons"] = card_actions
            return {
                "version": "2.0",
                "template": {
                    "outputs": [
                        {
                            "basicCard": basic_card,
                        }
                    ]
                },
            }
        components = _rich_form_components(payload.get("richForm"))
        if not components:
            continue
        image_component = next(
            (
                component
                for component in components
                if str(component.get("type") or "").strip().upper() == "IMAGE"
                and _is_public_absolute_url(component.get("url"))
            ),
            None,
        )
        if image_component is None:
            continue
        form_title = next(
            (
                str(component.get("title") or "").strip()
                for component in components
                if str(component.get("type") or "").strip().upper() == "FORM_TITLE"
                and str(component.get("title") or "").strip()
            ),
            "",
        )
        text_description = next(
            (
                str(component.get("text") or "").strip()
                for component in components
                if str(component.get("type") or "").strip().upper() == "TEXT"
                and str(component.get("text") or "").strip()
            ),
            "",
        )
        image_title = str(image_component.get("title") or "").strip()
        image_text = str(image_component.get("text") or "").strip()
        image_url = str(image_component.get("url") or "").strip()
        link_url = str(image_component.get("link") or "").strip()
        card_title = image_title or form_title or str(message.get("text") or "").strip() or "이미지"
        card_description = image_text or text_description or str(message.get("text") or "").strip()
        basic_card: dict[str, Any] = {
            "thumbnail": {"imageUrl": image_url, "fixedRatio": True},
            "title": card_title,
        }
        if card_description and card_description != card_title:
            basic_card["description"] = card_description
        card_actions: list[dict[str, str]] = []
        if _is_public_absolute_url(link_url):
            detail_action = _kakao_action_item(label="자세히 보기", url=link_url)
            if detail_action is not None:
                card_actions.append(detail_action)
        card_actions.extend(_rich_form_button_actions(payload.get("richForm")))
        card_actions = _dedupe_kakao_actions(card_actions, limit=3)
        if card_actions:
            basic_card["buttons"] = card_actions
        return {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "basicCard": basic_card,
                    }
                ]
            },
        }
    return None


def _kakao_carousel_response_from_serialized_messages(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in messages:
        payload = _as_dict(message.get("payload"))
        carousel_payload = _as_dict(payload.get("carousel"))
        if not carousel_payload:
            continue
        raw_image_url = str(carousel_payload.get("imageUrl") or "").strip()
        image_url = _kakao_public_image_url(raw_image_url)
        if raw_image_url and not image_url:
            continue
        card_title = (
            str(carousel_payload.get("itemTitle") or "").strip()
            or str(carousel_payload.get("title") or "").strip()
            or str(message.get("text") or "").strip()
            or "안내"
        )
        card_description = str(carousel_payload.get("itemContents") or "").strip()
        basic_card: dict[str, Any] = {
            "title": card_title,
        }
        if image_url:
            basic_card["thumbnail"] = {"imageUrl": image_url, "fixedRatio": False}
        if card_description and card_description != card_title:
            basic_card["description"] = card_description

        item_action: dict[str, str] | None = None
        button_type = str(carousel_payload.get("buttonType") or "").strip().lower()
        if button_type == "button":
            item_action = _kakao_action_item(
                label=carousel_payload.get("itemButtonLabel"),
                message_text=carousel_payload.get("itemButtonValue"),
            )
        card_actions = _dedupe_kakao_actions([item_action] if item_action is not None else [], limit=3)
        if card_actions:
            basic_card["buttons"] = card_actions

        response: dict[str, Any] = {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "carousel": {
                            "type": "basicCard",
                            "items": [basic_card],
                        }
                    }
                ]
            },
        }

        bottom_action = _kakao_action_item(
            label=carousel_payload.get("bottomButtonLabel"),
            message_text=carousel_payload.get("bottomButtonValue"),
        )
        if bottom_action is not None:
            response["template"]["quickReplies"] = [_kakao_quick_reply(bottom_action)]
        return response
    return None


def _kakao_response_from_serialized_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    list_card_response = _kakao_list_card_response_from_serialized_messages(messages)
    if list_card_response is not None:
        return list_card_response
    carousel_response = _kakao_carousel_response_from_serialized_messages(messages)
    if carousel_response is not None:
        return carousel_response
    basic_card_response = _kakao_basic_card_from_serialized_messages(messages)
    if basic_card_response is not None:
        return basic_card_response
    text_parts: list[str] = []
    quick_reply_actions: list[dict[str, str]] = []
    for message in messages:
        text = str(message.get("text") or "").strip()
        if text:
            text_parts.append(text)
        payload = _as_dict(message.get("payload"))
        actions = _message_actions_from_options(payload.get("options"))
        if not actions and payload.get("richForm") is not None:
            actions = _rich_form_button_actions(payload.get("richForm"))
        if actions:
            quick_reply_actions = actions
    text_value = "\n\n".join(text_parts).strip() or " "
    response = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text_value,
                    }
                }
            ]
        },
    }
    deduped_actions = _dedupe_kakao_actions(quick_reply_actions, limit=10)
    if deduped_actions:
        response["template"]["quickReplies"] = [_kakao_quick_reply(option) for option in deduped_actions]
    return response


def _kakao_response_log_summary(response: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    template = _as_dict(response.get("template"))
    outputs = _as_list(template.get("outputs"))
    output_types: list[str] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if value:
                output_types.append(str(key))
    if not output_types:
        output_types = ["simpleText"]
    fallback_reasons = _kakao_fallback_reasons(messages or [], output_types)
    return {
        "output_types": output_types,
        "quick_reply_count": len([item for item in _as_list(template.get("quickReplies")) if isinstance(item, dict)]),
        "fallback_used": bool(fallback_reasons),
        "fallback_reasons": fallback_reasons,
    }


def _channel_health_summary() -> list[dict[str, Any]]:
    supported = sorted(SUPPORTED_CHANNELS)
    summaries: list[dict[str, Any]] = []
    for channel in supported:
        item: dict[str, Any] = {
            "channel": channel,
            "status": "ok",
        }
        if channel == "kakao":
            item.update(
                {
                    "provider": "kakao",
                    "renderer": "kakao",
                    "webhook_endpoint": "/api/v1/channels/kakao/webhook",
                    "auth_header": "X-Aidot-Channel-Token",
                    "supported_outputs": ["simpleText", "quickReplies", "basicCard", "listCard", "carousel"],
                    "logging_events": [
                        "kakao.webhook.received",
                        "kakao.webhook.rejected",
                        "kakao.webhook.responded",
                        "kakao.webhook.failed",
                    ],
                }
            )
        summaries.append(item)
    return summaries


def _persistent_room_ids():
    from app.models import ChannelLlmBinding
    return select(ChannelLlmBinding.room_id)


def _is_persistent_room(db: Session, room_id: UUID | None) -> bool:
    if room_id is None:
        return False
    from app.models import ChannelLlmBinding
    return db.scalar(select(ChannelLlmBinding.id).where(ChannelLlmBinding.room_id == room_id).limit(1)) is not None



# Webchat 자격 증명을 검증한 서버 호출 범위에서만 한 방을 허용한다.
_webchat_room_capability: ContextVar[UUID | None] = ContextVar("webchat_room_capability", default=None)


# 검증된 공유방 참가자에 대해서만 사용하는 서버 내부 실행 권한이다.
_shared_turn_capability: ContextVar[dict | None] = ContextVar("shared_turn_capability", default=None)


@contextmanager
def _shared_room_turn_access(room_id, actor_id, generation, participant_name):
    context = {"room_id": room_id, "actor_id": actor_id, "generation": generation,
        "participant_name": participant_name, "fenced": False, "protected": {}, "queue_event_id": None, "lease_lost": False}
    token = _shared_turn_capability.set(context)
    try:
        yield context
    finally:
        _shared_turn_capability.reset(token)


@contextmanager
def _shared_room_execution_db(db):
    previous = db.expire_on_commit
    if _shared_turn_capability.get() is not None:
        db.expire_on_commit = False
    try:
        yield
    finally:
        db.expire_on_commit = previous


def _fence_shared_room_write(db, room):
    context = _shared_turn_capability.get()
    if context is None or context["room_id"] != room.id:
        return
    from app.services.room_turn import lock_current_room_turn
    candidate = _as_dict(room.metadata_json)
    if context["lease_lost"]:
        raise HTTPException(status_code=409, detail="질문 차례가 만료되어 이전 결과를 저장하지 않았습니다.")
    if not context["fenced"]:
        with db.no_autoflush:
            current = lock_current_room_turn(db, room.id, context["actor_id"], context["generation"])
        if current is None:
            raise HTTPException(status_code=409, detail="질문 차례가 변경되어 이전 결과를 저장하지 않았습니다.")
        context["protected"] = {key: deepcopy(_as_dict(current.metadata_json).get(key)) for key in ("__webchatAccess", "__roomTurn")}
        # 외부 응답 중 저장된 일반 대화의 이력은 최신 DB를 유지한다.
        history = deepcopy(_as_dict(candidate.get("conversationHistory")))
        latest_history = _as_dict(_as_dict(current.metadata_json).get("conversationHistory"))
        for key in ("message_count", "user_message_count", "transcript", "last_message_at", "last_user_message_at",
                    "started_at", "user_utterances", "user_raw_utterances", "first_user_utterance"):
            if key in latest_history:
                history[key] = deepcopy(latest_history[key])
        if history:
            candidate["conversationHistory"] = history
        context["fenced"] = True
    candidate.update(deepcopy(context["protected"]))
    written_history = context.get("written_history")
    if written_history is not None:
        history = _as_dict(candidate.get("conversationHistory"))
        for key in ("message_count", "user_message_count", "transcript", "last_message_at", "last_user_message_at",
                    "started_at", "user_utterances", "user_raw_utterances", "first_user_utterance"):
            if key in written_history:
                history[key] = deepcopy(written_history[key])
        candidate["conversationHistory"] = history
    room.metadata_json = candidate
    flag_modified(room, "metadata_json")


def _is_credential_room(room: ChannelRoom) -> bool:
    return str(getattr(room, "client_room_id", None) or "").startswith("wc-auth:") or "__webchatAccess" in _as_dict(getattr(room, "metadata_json", None))


def _credential_room_ids():
    return select(ChannelRoom.id).where(
        ChannelRoom.client_room_id.like("wc-auth:%") | ChannelRoom.metadata_json.has_key("__webchatAccess")
    )


@contextmanager
def _webchat_room_access(room_id: UUID):
    token = _webchat_room_capability.set(room_id)
    try:
        yield
    finally:
        _webchat_room_capability.reset(token)


def _assert_legacy_room_access(db: Session, room_id: UUID | None) -> None:
    if _is_persistent_room(db, room_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채팅방을 찾을 수 없습니다.")
    room = db.get(ChannelRoom, room_id) if room_id else None
    if room is not None and _is_credential_room(room):
        if _webchat_room_capability.get() != room.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채팅방을 찾을 수 없습니다.")


def _verify_channel_key(api_key: str | None) -> None:
    expected_key = settings.webchat_api_key.strip()
    if not expected_key:
        return
    if not api_key or api_key.strip() != expected_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="채널 API 인증이 필요합니다.")


def _ensure_channel(channel_type: str) -> str:
    normalized = channel_type.strip().lower()
    if normalized not in SUPPORTED_CHANNELS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="지원하지 않는 채널입니다.")
    return normalized


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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


def _answer_rag_variable_prefix(ai_config: dict[str, Any]) -> str:
    answer_mode = str(ai_config.get("answer_mode") or ai_config.get("answerMode") or "").strip()
    if answer_mode == "semantic_rag":
        return "_semantic"
    if answer_mode == "llm_rag":
        return "_llm"
    return ""


def _answer_variable_prefix(ai_config: dict[str, Any]) -> str:
    answer_mode = str(ai_config.get("answer_mode") or ai_config.get("answerMode") or "").strip()
    if answer_mode == "semantic_rag":
        return "_semantic"
    if answer_mode in {"llm", "llm_rag"}:
        return "_llm"
    return ""


def _answer_rag_variable_prefixes(prefix: str) -> list[str]:
    if prefix == "_semantic":
        return ["_semantic", "_rag"]
    return [prefix] if prefix else []


def _answer_match_value(
    match: Any,
    *,
    fallback_intent_id: str = "",
    fallback_intent_name: str = "",
) -> dict[str, Any]:
    if isinstance(match, dict):
        metadata = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
        intent_id = str(match.get("intentId") or match.get("intent_id") or metadata.get("intentId") or metadata.get("intent_id") or fallback_intent_id or "").strip()
        intent_name = str(match.get("intentName") or match.get("intent_name") or metadata.get("intentName") or metadata.get("intent_name") or fallback_intent_name or "").strip()
        source_type = str(match.get("sourceType") or match.get("source_type") or metadata.get("sourceType") or metadata.get("source_type") or "").strip()
        source_title = str(
            match.get("sourceTitle")
            or match.get("source_title")
            or metadata.get("sourceTitle")
            or metadata.get("source_title")
            or metadata.get("fileName")
            or metadata.get("file_name")
            or match.get("title")
            or ""
        ).strip()
        page = str(match.get("page") or metadata.get("page") or metadata.get("pageNo") or metadata.get("page_no") or "").strip()
        return {
            "documentId": str(match.get("documentId") or match.get("document_id") or "").strip(),
            "title": str(match.get("title") or "").strip(),
            "text": str(match.get("text") or "").strip(),
            "score": round(float(match.get("score") or 0.0), 6),
            "intentId": intent_id,
            "intentName": intent_name,
            "sourceType": source_type,
            "sourceTitle": source_title,
            "page": page,
            "metadata": metadata,
        }

    metadata = match.metadata if isinstance(getattr(match, "metadata", None), dict) else {}
    intent_id = str(metadata.get("intentId") or metadata.get("intent_id") or fallback_intent_id or "").strip()
    intent_name = str(metadata.get("intentName") or metadata.get("intent_name") or fallback_intent_name or "").strip()
    source_type = str(metadata.get("sourceType") or metadata.get("source_type") or "").strip()
    source_title = str(
        metadata.get("sourceTitle")
        or metadata.get("source_title")
        or metadata.get("fileName")
        or metadata.get("file_name")
        or getattr(match, "title", "")
        or ""
    ).strip()
    page = str(metadata.get("page") or metadata.get("pageNo") or metadata.get("page_no") or "").strip()
    return {
        "documentId": str(getattr(match, "document_id", "") or ""),
        "title": str(getattr(match, "title", "") or ""),
        "text": str(getattr(match, "text", "") or ""),
        "score": round(float(getattr(match, "score", 0.0) or 0.0), 6),
        "intentId": intent_id,
        "intentName": intent_name,
        "sourceType": source_type,
        "sourceTitle": source_title,
        "page": page,
        "metadata": metadata,
    }


def _answer_intent_name_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _answer_training_state(version: BotVersion) -> dict[str, Any]:
    version_json = normalize_version_document(getattr(version, "version_json", None))
    system_config = _as_dict(version_json.get("system_config"))
    return _as_dict(system_config.get("answer_training"))


def _answer_training_embedding_options(version: BotVersion) -> dict[str, str]:
    answer_training = _answer_training_state(version)
    return {
        "embedding_provider": str(answer_training.get("embedding_provider") or answer_training.get("embeddingProvider") or "").strip(),
        "embedding_model": str(answer_training.get("embedding_model") or answer_training.get("embeddingModel") or "").strip(),
    }


def _llm_answer_base_prompt(bot: Bot, version: BotVersion, selected_dialog: dict[str, Any]) -> str:
    dialog_prompt = str(
        selected_dialog.get("llmAnswerPrompt")
        or selected_dialog.get("llm_answer_prompt")
        or selected_dialog.get("answerPrompt")
        or selected_dialog.get("answer_prompt")
        or ""
    ).strip()
    if dialog_prompt:
        return dialog_prompt

    conversation_defaults = _conversation_defaults(bot, version)
    llm_answer = _as_dict(conversation_defaults.get("llmAnswer"))
    settings_prompt = str(
        llm_answer.get("systemPrompt")
        or llm_answer.get("system_prompt")
        or llm_answer.get("prompt")
        or ""
    ).strip()
    return settings_prompt


def _llm_answer_system_prompt(
    bot: Bot,
    version: BotVersion,
    selected_dialog: dict[str, Any],
    *,
    rag: bool,
) -> str:
    parts = [_llm_answer_base_prompt(bot, version, selected_dialog) or DEFAULT_LLM_ANSWER_SYSTEM_PROMPT]
    if rag:
        parts.append(LLM_RAG_ANSWER_SAFETY_PROMPT)
    parts.append(LLM_JSON_ANSWER_FORMAT_PROMPT)
    return " ".join(part.strip() for part in parts if part and part.strip())


def _precomputed_answer_matches(version: BotVersion, selected_dialog: dict[str, Any]) -> list[dict[str, Any]]:
    answer_training = _answer_training_state(version)
    precomputed = _as_dict(answer_training.get("precomputed_answers"))
    by_intent_id = _as_dict(precomputed.get("by_intent_id"))
    by_intent_name = _as_dict(precomputed.get("by_intent_name"))
    intent_id = str(selected_dialog.get("id") or "").strip()
    intent_name = str(selected_dialog.get("name") or selected_dialog.get("displayName") or "").strip()
    value = by_intent_id.get(intent_id) if intent_id else None
    if not isinstance(value, dict):
        value = by_intent_name.get(_answer_intent_name_key(intent_name)) if intent_name else None
    return [_answer_match_value(value, fallback_intent_id=intent_id, fallback_intent_name=intent_name)] if isinstance(value, dict) else []


def _clear_answer_rag_variables(variables: dict[str, Any], prefix: str) -> None:
    for item_prefix in _answer_rag_variable_prefixes(prefix):
        _set_variable(variables, f"${item_prefix}_answers", [])
        _set_variable(variables, f"${item_prefix}_answer_text", "")
        _set_variable(variables, f"${item_prefix}_answer_score", "")
        _set_variable(variables, f"${item_prefix}_answer_intent_id", "")
        _set_variable(variables, f"${item_prefix}_answer_intent_name", "")
        _set_variable(variables, f"${item_prefix}_answer_source_type", "")
        _set_variable(variables, f"${item_prefix}_answer_source_title", "")
        _set_variable(variables, f"${item_prefix}_answer_page", "")


def _set_answer_rag_variables(
    variables: dict[str, Any],
    prefix: str,
    matches: list[Any],
    *,
    fallback_intent_id: str = "",
    fallback_intent_name: str = "",
) -> None:
    values = [
        _answer_match_value(
            match,
            fallback_intent_id=fallback_intent_id,
            fallback_intent_name=fallback_intent_name,
        )
        for match in matches
    ]
    first = values[0] if values else {}
    for item_prefix in _answer_rag_variable_prefixes(prefix):
        _set_variable(variables, f"${item_prefix}_answers", values)
        _set_variable(variables, f"${item_prefix}_answer_text", str(first.get("text") or ""))
        _set_variable(variables, f"${item_prefix}_answer_score", "" if not first else f"{float(first.get('score') or 0.0):.4f}")
        _set_variable(variables, f"${item_prefix}_answer_intent_id", str(first.get("intentId") or ""))
        _set_variable(variables, f"${item_prefix}_answer_intent_name", str(first.get("intentName") or ""))
        _set_variable(variables, f"${item_prefix}_answer_source_type", str(first.get("sourceType") or ""))
        _set_variable(variables, f"${item_prefix}_answer_source_title", str(first.get("sourceTitle") or ""))
        _set_variable(variables, f"${item_prefix}_answer_page", str(first.get("page") or ""))


def _generate_constrained_llm_rag_answer(
    *,
    ai_config: dict[str, Any],
    system_prompt: str,
    query: str,
    matches: list[Any],
    fallback_intent_id: str = "",
    fallback_intent_name: str = "",
) -> str:
    values = [
        _answer_match_value(
            match,
            fallback_intent_id=fallback_intent_id,
            fallback_intent_name=fallback_intent_name,
        )
        for match in matches
    ]
    contexts = []
    for index, value in enumerate(values[:3], start=1):
        text = str(value.get("text") or "").strip()
        if not text:
            continue
        contexts.append(
            {
                "index": index,
                "title": str(value.get("title") or value.get("sourceTitle") or "").strip(),
                "score": value.get("score"),
                "text": text[:2500],
            }
        )
    if not contexts:
        return ""
    user_prompt = json.dumps(
        {
            "user_question": query,
            "rag_context": contexts,
        },
        ensure_ascii=False,
    )
    provider = str(ai_config.get("llm_provider") or "").strip()
    model = str(ai_config.get("llm_model") or "").strip()
    base_url = str(ai_config.get("llm_base_url") or "").strip()
    config = resolve_llm_provider_config(provider, model, base_url=base_url)
    client = LlmChatClient(config)
    result = client.chat(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=True)
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", result.content, flags=re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    answer = str(parsed.get("answer") if isinstance(parsed, dict) else "").strip()
    return answer


def _generate_llm_answer(
    *,
    ai_config: dict[str, Any],
    system_prompt: str,
    query: str,
    intent_id: str = "",
    intent_name: str = "",
) -> str:
    user_prompt_parts = [f"사용자 질문: {query}"]
    if intent_id or intent_name:
        user_prompt_parts.append(f"매칭 의도 ID: {intent_id}")
        user_prompt_parts.append(f"매칭 의도명: {intent_name}")
    user_prompt_parts.append("위 사용자 질문에 대한 답변을 생성하세요.")
    user_prompt = "\n".join(part for part in user_prompt_parts if part.strip())
    provider = str(ai_config.get("llm_provider") or "").strip()
    model = str(ai_config.get("llm_model") or "").strip()
    base_url = str(ai_config.get("llm_base_url") or "").strip()
    config = resolve_llm_provider_config(provider, model, base_url=base_url)
    client = LlmChatClient(config)
    result = client.chat(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=True)
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", result.content, flags=re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    return str(parsed.get("answer") if isinstance(parsed, dict) else "").strip()


def _with_constrained_llm_rag_answer(
    *,
    ai_config: dict[str, Any],
    system_prompt: str,
    query: str,
    matches: list[Any],
    fallback_intent_id: str = "",
    fallback_intent_name: str = "",
) -> list[Any]:
    if _answer_rag_variable_prefix(ai_config) != "_llm" or not matches:
        return matches
    values = [
        _answer_match_value(
            match,
            fallback_intent_id=fallback_intent_id,
            fallback_intent_name=fallback_intent_name,
        )
        for match in matches
    ]
    try:
        answer = _generate_constrained_llm_rag_answer(
            ai_config=ai_config,
            system_prompt=system_prompt,
            query=query,
            matches=matches,
            fallback_intent_id=fallback_intent_id,
            fallback_intent_name=fallback_intent_name,
        )
    except (LlmClientError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "LLM RAG 답변 생성에 실패했습니다.",
            extra={
                "event": "channel.runtime.llm_rag_answer_generation_failed",
                "detail": str(exc),
            },
        )
        return matches
    if not answer or not values:
        return matches
    first = dict(values[0])
    metadata = dict(first.get("metadata") if isinstance(first.get("metadata"), dict) else {})
    metadata["ragGenerated"] = True
    metadata["ragGenerationProvider"] = str(ai_config.get("llm_provider") or "")
    metadata["ragGenerationModel"] = str(ai_config.get("llm_model") or "")
    first["metadata"] = metadata
    first["text"] = answer
    return [first, *values[1:]]


def _prepare_answer_rag_variables(
    runtime_state: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    selected_dialog: dict[str, Any],
) -> None:
    ai_config = _version_ai_config(bot, version)
    answer_mode = str(ai_config.get("answer_mode") or ai_config.get("answerMode") or "").strip()
    prefix = _answer_variable_prefix(ai_config)
    if not prefix:
        return

    variables = _as_dict(runtime_state.get("variables"))
    runtime_state["variables"] = variables
    _clear_answer_rag_variables(variables, prefix)

    intent_id = str(selected_dialog.get("id") or "").strip()
    intent_name = str(selected_dialog.get("name") or selected_dialog.get("displayName") or "").strip()
    if answer_mode == "llm":
        try:
            answer = _generate_llm_answer(
                ai_config=ai_config,
                system_prompt=_llm_answer_system_prompt(bot, version, selected_dialog, rag=False),
                query=message,
                intent_id=intent_id,
                intent_name=intent_name,
            )
        except (LlmClientError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "LLM 답변 생성에 실패했습니다.",
                extra={
                    "event": "channel.runtime.llm_answer_generation_failed",
                    "bot_id": str(bot.id),
                    "version_id": str(version.id),
                    "detail": str(exc),
                },
            )
            return
        if answer:
            _set_answer_rag_variables(
                variables,
                prefix,
                [{
                    "text": answer,
                    "score": 1.0,
                    "intentId": intent_id,
                    "intentName": intent_name,
                    "sourceType": "llm",
                    "sourceTitle": "LLM Engine 답변",
                    "metadata": {
                        "llmGenerated": True,
                        "llmProvider": str(ai_config.get("llm_provider") or ""),
                        "llmModel": str(ai_config.get("llm_model") or ""),
                    },
                }],
                fallback_intent_id=intent_id,
                fallback_intent_name=intent_name,
            )
        return

    prefix = _answer_rag_variable_prefix(ai_config)
    if not prefix:
        return
    precomputed_matches = _precomputed_answer_matches(version, selected_dialog)
    if precomputed_matches:
        _set_answer_rag_variables(
            variables,
            prefix,
            precomputed_matches,
            fallback_intent_id=intent_id,
            fallback_intent_name=intent_name,
        )
        logger.info(
            "Answer RAG precomputed answer selected.",
            extra={
                "event": "channel.runtime.answer_rag_precomputed",
                "bot_id": str(bot.id),
                "version_id": str(version.id),
                "prefix": prefix,
                "intent_id": intent_id,
            },
        )
        return

    config = answer_vector_config(ai_config)
    if not config.is_ready:
        logger.warning(
            "Answer Vector DB 연결 설정이 완료되지 않았습니다.",
            extra={"event": "channel.runtime.answer_rag_unconfigured", "missing_fields": config.missing_fields},
        )
        return

    client = AnswerVectorSearchClient(config)
    embedding_options = _answer_training_embedding_options(version)
    top_k = 3
    scoped_elapsed_ms = 0.0
    fallback_elapsed_ms = 0.0
    used_fallback = False
    try:
        scoped_started_at = time.perf_counter()
        matches = client.search(
            bot_id=str(bot.id),
            version_id=str(version.id),
            query=message,
            top_k=top_k,
            intent_ids=[intent_id] if intent_id else None,
            embedding_provider=embedding_options["embedding_provider"],
            embedding_model=embedding_options["embedding_model"],
        )
        scoped_elapsed_ms = _elapsed_ms(scoped_started_at)
        if intent_id and not matches:
            used_fallback = True
            fallback_started_at = time.perf_counter()
            matches = client.search(
                bot_id=str(bot.id),
                version_id=str(version.id),
                query=message,
                top_k=top_k,
                intent_ids=None,
                embedding_provider=embedding_options["embedding_provider"],
                embedding_model=embedding_options["embedding_model"],
            )
            fallback_elapsed_ms = _elapsed_ms(fallback_started_at)
    except VectorSearchError as exc:
        logger.warning(
            "Answer Vector DB 검색에 실패했습니다.",
            extra={
                "event": "channel.runtime.answer_rag_search_failed",
                "bot_id": str(bot.id),
                "version_id": str(version.id),
                "detail": str(exc),
            },
        )
        return

    total_elapsed_ms = round(scoped_elapsed_ms + fallback_elapsed_ms, 2)
    log_payload = {
        "event": "channel.runtime.answer_rag_search_completed",
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "prefix": prefix,
        "query_length": len(message),
        "top_k": top_k,
        "intent_scoped": bool(intent_id),
        "used_fallback": used_fallback,
        "result_count": len(matches),
        "scoped_elapsed_ms": scoped_elapsed_ms,
        "fallback_elapsed_ms": fallback_elapsed_ms,
        "elapsed_ms": total_elapsed_ms,
    }
    if total_elapsed_ms >= settings.api_slow_request_threshold_ms:
        logger.warning("Answer RAG vector search slow.", extra=log_payload)
    else:
        logger.info("Answer RAG vector search completed.", extra=log_payload)

    answer_matches = _with_constrained_llm_rag_answer(
        ai_config=ai_config,
        system_prompt=_llm_answer_system_prompt(bot, version, selected_dialog, rag=True),
        query=message,
        matches=matches,
        fallback_intent_id=intent_id,
        fallback_intent_name=intent_name,
    )
    _set_answer_rag_variables(
        variables,
        prefix,
        answer_matches,
        fallback_intent_id=intent_id,
        fallback_intent_name=intent_name,
    )


def _nlu_training_state(version: BotVersion) -> dict[str, Any]:
    snapshot = getattr(version, "nlu_training_json", None)
    if isinstance(snapshot, dict):
        return snapshot
    version_json = normalize_version_document(getattr(version, "version_json", None))
    system_config = _as_dict(version_json.get("system_config"))
    return _as_dict(system_config.get("nlu_training"))


def _int_setting(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    if isinstance(value, str):
        try:
            return round(float(value))
        except ValueError:
            return default
    return default


def _first_text(values: Any) -> str:
    for value in _as_list(values):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _find_start_talk_node(graph: dict[str, Any]) -> dict[str, Any] | None:
    nodes = [node for node in _as_list(graph.get("nodes")) if isinstance(node, dict)]
    links = _graph_links(graph)
    start_node = next((node for node in nodes if node.get("kind") == "start"), None)
    if start_node is None:
        return next((node for node in nodes if node.get("kind") == "talk"), None)
    next_link = next((link for link in links if link.get("sourceNodeId") == start_node.get("id")), None)
    target_id = next_link.get("targetNodeId") if next_link else None
    return next((node for node in nodes if node.get("id") == target_id and node.get("kind") == "talk"), None)


def _initial_messages_for_version(
    bot: Bot,
    version: BotVersion,
    default_messages: dict[str, str] | None = None,
    channel_type: str | None = "webchat",
) -> list[dict[str, Any]]:
    default_messages = default_messages or {}
    document = normalize_version_document(version.version_json)
    version_settings = _version_settings(bot, version)
    messages = _as_dict(version_settings.get("messages"))
    greeting = _as_dict(messages.get("greeting"))

    if greeting.get("enabled") is False:
        return []

    mode = str(greeting.get("mode") or "").strip()
    value = str(greeting.get("value") or "").strip()
    if mode == "text" and value:
        return [{"type": "text", "text": value, "options": []}]

    if mode != "module" or not value:
        return []

    dialogs = [dialog for dialog in _as_list(document.get("dialogs")) if isinstance(dialog, dict)]
    greeting_dialog = next(
        (
            dialog
            for dialog in dialogs
            if str(dialog.get("name") or "") == value or str(dialog.get("displayName") or "") == value
        ),
        None,
    )
    graphs = [graph for graph in _as_list(document.get("dialog_flow_graphs")) if isinstance(graph, dict)]
    graph = next(
        (
            graph
            for graph in graphs
            if graph.get("dialogId") == (greeting_dialog or {}).get("id") or str(graph.get("name") or "") == value
        ),
        None,
    )
    if graph is None:
        return []

    talk_node = _find_start_talk_node(graph)
    if talk_node is None:
        return []
    channel_code = _channel_template_code(channel_type)
    state = _initial_runtime_state_for_version(bot, version, channel_type)
    variables = _as_dict(state.get("variables"))
    output = _talk_output(talk_node, variables, default_messages, channel_code, state, graph)
    if output is None:
        return []
    return [
        {
            "type": str(output.get("type") or "text"),
            "text": str(output.get("text") or ""),
            "options": output.get("options") or [],
            "payload": _as_dict(output.get("payload")),
        }
    ]

def _channel_admin_code(channel_type: str | None) -> str:
    normalized = (channel_type or "webchat").strip().lower()
    return {
        "webchat": "WEBCHAT",
        "kakao": "KAKAO",
        "ms-teams": "TEAMS",
    }.get(normalized, normalized.upper())


def _get_active_admin_channel(db: Session, organization_id: Any, channel_type: str) -> AdminChannel | None:
    return db.scalar(
        select(AdminChannel).where(
            AdminChannel.organization_id == organization_id,
            AdminChannel.code == _channel_admin_code(channel_type),
            AdminChannel.status == "active",
            AdminChannel.deleted_at.is_(None),
        )
    )


def _botstation_settings(bot: Bot, version: BotVersion) -> dict[str, Any]:
    return _as_dict(_version_settings(bot, version).get("botstation"))


def _conversation_defaults(bot: Bot, version: BotVersion) -> dict[str, Any]:
    return _as_dict(_version_settings(bot, version).get("conversationDefaults"))


def _exacting_matching_enabled(bot: Bot, version: BotVersion) -> bool:
    conversation_defaults = _conversation_defaults(bot, version)
    exacting_matching = _as_dict(conversation_defaults.get("exactingMatching"))
    return exacting_matching.get("enabled") is not False


def _intent_detection_settings(bot: Bot, version: BotVersion) -> dict[str, Any]:
    conversation_defaults = _conversation_defaults(bot, version)
    return _as_dict(conversation_defaults.get("intentDetection"))


def _button_selection_option(bot: Bot, version: BotVersion) -> str:
    conversation_defaults = _conversation_defaults(bot, version)
    button_selection = _as_dict(conversation_defaults.get("buttonSelection"))
    option = str(button_selection.get("option") or "exact").strip().lower()
    return "contains" if option == "contains" else "exact"


def _nlu_cutoff_score(bot: Bot, version: BotVersion) -> float:
    conversation_defaults = _conversation_defaults(bot, version)
    ml_settings = _as_dict(conversation_defaults.get("ml"))
    value = ml_settings.get("cutOffScore")
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.75
    return max(0.0, min(0.99, score))


def _version_settings(bot: Bot, version: BotVersion) -> dict[str, Any]:
    data_json = _as_dict(bot.data_json)
    settings_by_version = _as_dict(data_json.get("settings_by_version"))
    version_id = str(getattr(version, "id", "") or "")
    version_name = str(getattr(version, "name", "") or "")
    return _as_dict(settings_by_version.get(version_id)) or _as_dict(settings_by_version.get(version_name))


def _apply_blocklist_patterns(bot: Bot, version: BotVersion, message: str) -> tuple[str, list[dict[str, Any]]]:
    result = str(message or "")
    applied: list[dict[str, Any]] = []
    for item in _as_list(_version_settings(bot, version).get("blocklists")):
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            continue
        try:
            if str(item.get("type") or "word") == "regex":
                next_result = _replace_runtime_regex(pattern, result)
            else:
                next_result = re.sub(re.escape(pattern), " ", result, flags=re.IGNORECASE)
        except re.error:
            continue
        if next_result != result:
            applied.append(item)
            result = next_result
    return _normalize_text(result), applied


def _runtime_regex_parts(pattern: str) -> tuple[str, int]:
    raw_pattern = pattern.strip()
    literal_match = re.fullmatch(r"/(.+)/([a-zA-Z]*)", raw_pattern)
    source = literal_match.group(1) if literal_match else raw_pattern
    flags_text = literal_match.group(2) if literal_match else "i"
    flags = re.IGNORECASE if "i" in flags_text.lower() or not literal_match else 0
    return source, flags


def _replace_runtime_regex(pattern: str, message: str) -> str:
    source, flags = _runtime_regex_parts(pattern)
    return re.sub(source, " ", message, flags=flags)

def _smalltalk_match(bot: Bot, version: BotVersion, message: str) -> dict[str, Any] | None:
    smalltalk = _as_dict(_version_settings(bot, version).get("smalltalk"))
    if smalltalk.get("enabled") is False:
        return None
    normalized_message = _normalize_text(message)
    for item in _as_list(smalltalk.get("items")):
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        utterances = _smalltalk_text_values(item.get("userMessages"), item.get("utterance"))
        responses = _smalltalk_text_values(item.get("botMessages"), item.get("response"))
        matched_utterance = next(
            (utterance for utterance in utterances if _normalize_text(utterance) == normalized_message),
            "",
        )
        if matched_utterance and responses:
            matched_item = dict(item)
            matched_item["utterance"] = matched_utterance
            matched_item["response"] = random.choice(responses)
            matched_item["matchedUtterance"] = matched_utterance
            return matched_item
    return None


def _smalltalk_text_values(value: Any, fallback: Any = "") -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        raw_values = [value]
    else:
        raw_values = []

    values: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        text = str(raw_value or "").strip()
        if text and text not in seen:
            seen.add(text)
            values.append(text)

    fallback_text = str(fallback or "").strip()
    if not values and fallback_text:
        values.append(fallback_text)
    return values


def _rule_expression_matches(expression: object, message: str) -> bool:
    value = str(expression or "").strip()
    if not value:
        return False
    try:
        source, flags = _runtime_regex_parts(value)
        return re.search(source, message, flags=flags) is not None
    except re.error:
        return _normalize_text(value) in _normalize_text(message)


def _hub_call_rules(bot: Bot) -> list[dict[str, Any]]:
    data = _as_dict(bot.data_json)
    return [item for item in _as_list(data.get("hub_call_rules")) if isinstance(item, dict)]


def _find_hub_call_rule(
    db: Session,
    source_bot: Bot,
    message: str,
) -> tuple[Bot, BotVersion, dict[str, Any]] | None:
    candidates = db.execute(
        select(Bot, BotVersion)
        .join(BotHub, BotHub.bot_id == Bot.id)
        .join(BotVersion, BotVersion.id == Bot.active_version_id)
        .where(
            Bot.organization_id == source_bot.organization_id,
            Bot.group_id == source_bot.group_id,
            Bot.deleted_at.is_(None),
            Bot.status == "active",
            BotVersion.status == "active",
        )
        .order_by(Bot.name.asc())
    ).all()
    for hub_bot, hub_version in candidates:
        for rule in _hub_call_rules(hub_bot):
            if rule.get("enabled") is False:
                continue
            if _rule_expression_matches(rule.get("expression"), message):
                return hub_bot, hub_version, rule
    return None


def _rule_match(bot: Bot, version: BotVersion, message: str) -> dict[str, Any] | None:
    for item in _as_list(_version_settings(bot, version).get("rules")):
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        expression = str(item.get("expression") or "").strip()
        target = str(item.get("target") or "").strip()
        if not expression or not target:
            continue
        if _rule_expression_matches(expression, message):
            return item
    return None


def _exact_utterance_dialog_for_bot(document: dict[str, Any], bot: Bot, version: BotVersion, message: str) -> dict[str, Any] | None:
    data_json = _version_ai_config(bot, version)
    nlu_type = str(data_json.get("nlu_type") or data_json.get("nluType") or "ml")
    if nlu_type in {"semantic", "semantic_vector", "semantic_external"}:
        dictionary_terms = _dictionary_terms_from_version(version.version_json)
        return _exact_utterance_dialog(
            document,
            message,
            canonical_map=_dictionary_canonical_map(dictionary_terms),
        )
    return _exact_utterance_dialog(document, message)


def _botstation_channel_connection(bot: Bot, version: BotVersion, channel_type: str) -> dict[str, Any] | None:
    botstation = _botstation_settings(bot, version)
    if not botstation:
        return {
            "channelCode": _channel_admin_code(channel_type),
            "enabled": True,
            "botIdentifier": str(bot.id),
            "source": "legacy-default",
        }
    if botstation.get("connected") is not True or botstation.get("enabled") is not True:
        return None
    channel_code = _channel_admin_code(channel_type)
    for item in _as_list(botstation.get("channels")):
        if not isinstance(item, dict):
            continue
        if str(item.get("channelCode") or "").upper() != channel_code:
            continue
        if item.get("enabled") is not True:
            return None
        identifier = str(item.get("botIdentifier") or "").strip()
        if identifier and identifier != str(bot.id):
            return None
        return item
    return None


def _ensure_botstation_connection(db: Session, bot: Bot, version: BotVersion, channel_type: str) -> dict[str, Any]:
    if _get_active_admin_channel(db, bot.organization_id, channel_type) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="관리자 채널이 사용 상태가 아닙니다.")
    connection = _botstation_channel_connection(bot, version, channel_type)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="봇스테이션에 연결된 채널만 실행할 수 있습니다.")
    return connection


def _requires_botstation_connection(channel_type: str | None) -> bool:
    if channel_type is None:
        return False
    return _ensure_channel(channel_type) != "webchat"


def _get_active_bot_version(db: Session, bot_id: str, channel_type: str | None = None) -> tuple[Bot, BotVersion, Group | None]:
    try:
        normalized_bot_id = UUID(str(bot_id).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널 봇을 찾을 수 없습니다.") from None
    bot = db.scalar(
        select(Bot).where(
            Bot.id == normalized_bot_id,
            Bot.status == "active",
            Bot.deleted_at.is_(None),
        )
    )
    if bot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널 봇을 찾을 수 없습니다.")
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

    if _requires_botstation_connection(channel_type):
        _ensure_botstation_connection(db, bot, version, channel_type)
    _ensure_runtime_supported(bot, version)

    group = db.scalar(select(Group).where(Group.id == bot.group_id))
    return bot, version, group


def _list_active_channel_bots(
    db: Session,
    channel_type: str | None = None,
    *,
    include_runtime_blocked: bool = False,
) -> list[tuple[Bot, BotVersion, Group | None]]:
    bots = db.scalars(
        select(Bot).where(
            Bot.status == "active",
            Bot.active_version_id.is_not(None),
            Bot.deleted_at.is_(None),
        ).order_by(Bot.name.asc())
    ).all()
    items: list[tuple[Bot, BotVersion, Group | None]] = []
    for bot in bots:
        if _as_dict(bot.data_json).get("bot_kind") == "hub":
            continue
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
        if _requires_botstation_connection(channel_type):
            try:
                _ensure_botstation_connection(db, bot, version, channel_type)
            except HTTPException:
                continue
        if not include_runtime_blocked and _runtime_block_reason(bot, version):
            continue
        group = db.scalar(select(Group).where(Group.id == bot.group_id))
        items.append((bot, version, group))
    return items



def _load_default_messages(
    db: Session,
    organization_id: Any,
    languages: tuple[str, ...] = ("ko",),
) -> dict[str, str]:
    return {
        key: get_default_message_text(db, organization_id, key, languages=languages, fallback=fallback)
        for key, fallback in DEFAULT_MESSAGE_FALLBACKS.items()
    }


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

def _serialize_bot(
    db: Session,
    bot: Bot,
    version: BotVersion,
    group: Group | None,
    channel_type: str | None = "webchat",
    languages: tuple[str, ...] = ("ko",),
) -> dict[str, Any]:
    default_messages = _load_default_messages(db, bot.organization_id, languages)
    return {
        "id": str(bot.id),
        "name": bot.name,
        "groupId": str(bot.group_id),
        "groupName": group.name if group else None,
        "activeVersionId": str(version.id),
        "activeVersionName": version.name,
        "activeVersionNo": version.version_no,
        "activatedAt": _iso(version.activated_at),
        "initialMessages": _initial_messages_for_version(bot, version, default_messages, channel_type),
    }


def _serialize_room(db: Session, room: ChannelRoom) -> dict[str, Any]:
    bot = db.get(Bot, room.bot_id)
    version = db.get(BotVersion, room.bot_version_id)
    if bot is None or version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="채팅방의 봇 정보를 찾을 수 없습니다.")
    group = db.scalar(select(Group).where(Group.id == bot.group_id))
    languages = _message_language_candidates(None, None, bot, room)
    return {
        "id": str(room.id),
        "clientRoomId": room.client_room_id,
        "channelType": room.channel_type,
        "status": room.status,
        "bot": _serialize_bot(db, bot, version, group, room.channel_type, languages),
        "participant": {"id": room.participant_id, "name": room.participant_name},
        "createdAt": _iso(room.created_at),
        "updatedAt": _iso(room.updated_at),
    }


def _serialize_message(message: ChannelMessage) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "participantId": message.participant_id,
        "participantKind": message.participant_kind,
        "participantName": message.participant_name,
        "messageType": message.message_type,
        "text": message.text,
        "payload": message.payload_json,
        "createdAt": _iso(message.created_at),
    }


def _graph_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in _as_list(graph.get("nodes")) if isinstance(node, dict)]


def _graph_links(graph: dict[str, Any]) -> list[dict[str, Any]]:
    normalized_links: list[dict[str, Any]] = []
    seen_indexes: dict[tuple[str, str], int] = {}
    for link in _as_list(graph.get("links")):
        if not isinstance(link, dict):
            continue
        source_node_id = str(link.get("sourceNodeId") or "")
        source_port = str(link.get("sourcePort") or "next")
        if not source_node_id:
            continue
        key = (source_node_id, source_port)
        if key in seen_indexes:
            continue
        seen_indexes[key] = len(normalized_links)
        normalized_links.append(link)
    return normalized_links


def _find_graph(document: dict[str, Any], graph_id: str | None = None, dialog_id: str | None = None, name: str | None = None) -> dict[str, Any] | None:
    graphs = [graph for graph in _as_list(document.get("dialog_flow_graphs")) if isinstance(graph, dict)]
    for graph in graphs:
        if graph_id and str(graph.get("id") or "") == graph_id:
            return graph
        if dialog_id and str(graph.get("dialogId") or "") == dialog_id:
            return graph
        if name and str(graph.get("name") or "") == name:
            return graph
    return None


def _find_node(graph: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    return next((node for node in _graph_nodes(graph) if str(node.get("id") or "") == node_id), None)


def _next_node_id(graph: dict[str, Any], source_node_id: str, source_port: str = "next") -> str | None:
    link = next(
        (
            item
            for item in _graph_links(graph)
            if item.get("sourceNodeId") == source_node_id and str(item.get("sourcePort") or "next") == source_port
        ),
        None,
    )
    return str(link.get("targetNodeId")) if link and link.get("targetNodeId") else None


def _first_runtime_node_id(graph: dict[str, Any]) -> str | None:
    start_node = next((node for node in _graph_nodes(graph) if node.get("kind") == "start"), None)
    if start_node is not None:
        return _next_node_id(graph, str(start_node.get("id") or ""))
    first_node = next((node for node in _graph_nodes(graph) if node.get("kind") != "start"), None)
    return str(first_node.get("id")) if first_node else None


def _bot_hub_runtime_values(bot: Bot) -> tuple[str, str]:
    data_json = _as_dict(getattr(bot, "data_json", None))
    if str(data_json.get("bot_kind") or "").strip() == "hub":
        return str(getattr(bot, "id", "") or ""), str(getattr(bot, "name", "") or "")
    return (
        str(data_json.get("bot_hub_id") or data_json.get("hub_id") or ""),
        str(data_json.get("bot_hub_name") or data_json.get("hub_name") or ""),
    )


def _set_runtime_system_variables(
    state: dict[str, Any],
    *,
    bot: Bot,
    channel_code: str = "WEBCHAT",
    room: ChannelRoom | None = None,
    participant_id: str | None = None,
    participant_name: str | None = None,
    message: str | None = None,
) -> None:
    variables = _as_dict(state.get("variables"))
    state["variables"] = variables
    now = datetime.now(timezone.utc)
    session_id = str(getattr(room, "id", "") or _get_variable(variables, "_session_id") or "")
    user_id = str(participant_id or getattr(room, "participant_id", "") or "")
    user_name = str(participant_name or getattr(room, "participant_name", "") or "")
    bot_hub_id, bot_hub_name = _bot_hub_runtime_values(bot)
    if not bot_hub_id:
        bot_hub_id = str(_get_variable(variables, "_bot_hub_id") or "")
        bot_hub_name = str(_get_variable(variables, "_bot_hub_name") or "")
    if not str(_get_variable(variables, "_dialog_start_time") or "").strip():
        _set_variable(variables, "_dialog_start_time", now.isoformat())
    _set_variable(variables, "_bot_hub_id", bot_hub_id)
    _set_variable(variables, "_bot_hub_name", bot_hub_name)
    _set_variable(variables, "_bot_id", str(getattr(bot, "id", "") or ""))
    _set_variable(variables, "_bot_name", str(getattr(bot, "name", "") or ""))
    _set_variable(variables, "_channel_id", channel_code)
    _set_variable(variables, "_date_time", now.isoformat())
    _set_variable(variables, "_dialog_id", str(state.get("dialogId") or ""))
    _set_variable(variables, "_id", session_id)
    _set_variable(variables, "_session_id", session_id)
    _set_variable(variables, "_today", now.date().isoformat())
    _set_variable(variables, "_user_id", user_id)
    _set_variable(variables, "_user_name", user_name)
    if message is not None:
        _set_variable(variables, "_msg", message)


def _initial_runtime_state_for_version(bot: Bot, version: BotVersion, channel_type: str = "webchat") -> dict[str, Any]:
    document = normalize_version_document(version.version_json)
    version_settings = _version_settings(bot, version)
    conversation_defaults = _as_dict(version_settings.get("conversationDefaults"))
    intent_detection = _as_dict(conversation_defaults.get("intentDetection"))
    greeting = _as_dict(_as_dict(version_settings.get("messages")).get("greeting"))
    value = str(greeting.get("value") or "").strip()
    preprocess_module = str(intent_detection.get("preprocessModule") or "").strip()
    before_session_end_module = str(intent_detection.get("beforeSessionEndModule") or "").strip()
    graph: dict[str, Any] | None = None

    if preprocess_module:
        graph = _graph_for_module_name(document, preprocess_module)

    if graph is None and greeting.get("enabled") is not False and str(greeting.get("mode") or "").strip() == "module" and value:
        dialogs = [dialog for dialog in _as_list(document.get("dialogs")) if isinstance(dialog, dict)]
        greeting_dialog = next(
            (
                dialog
                for dialog in dialogs
                if str(dialog.get("name") or "") == value or str(dialog.get("displayName") or "") == value
            ),
            None,
        )
        graph = _find_graph(document, dialog_id=str((greeting_dialog or {}).get("id") or ""), name=value)

    if graph is None and (_as_dict(bot.data_json).get("bot_kind") == "hub" or not isinstance(_version_ai_config(bot, version).get("secondary_llm"), dict)):
        graph = next((item for item in _as_list(document.get("dialog_flow_graphs")) if isinstance(item, dict)), None)
    if graph is None:
        state = {"variables": {}, "__beforeSessionEndModule": before_session_end_module}
        _set_runtime_system_variables(state, bot=bot, channel_code=_channel_template_code(channel_type))
        return state

    start_node_id = _first_runtime_node_id(graph)
    state = {
        "graphId": str(graph.get("id") or ""),
        "dialogId": str(graph.get("dialogId") or ""),
        "currentNodeId": str(start_node_id or ""),
        "waitingNodeId": "",
        "variables": {},
        "__beforeSessionEndModule": before_session_end_module,
        "__runtimeTransitionLocked": False,
        "__runtimeReturnBlocked": False,
    }
    _set_runtime_system_variables(state, bot=bot, channel_code=_channel_template_code(channel_type))
    return state


def _var_key(value: str) -> str:
    return var_key(value)


def _runtime_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    events = state.get("runtimeEvents")
    if not isinstance(events, list):
        events = []
    state["runtimeEvents"] = events
    return events


def _runtime_node_data(graph: dict[str, Any] | None, node: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "graphId": str(_as_dict(graph).get("id") or ""),
        "dialogId": str(_as_dict(graph).get("dialogId") or ""),
        "dialogName": str(_as_dict(graph).get("name") or ""),
        "nodeId": str(_as_dict(node).get("id") or ""),
        "nodeKind": str(_as_dict(node).get("kind") or ""),
        "nodeTitle": str(_as_dict(node).get("title") or ""),
    }


def _append_runtime_event(
    state: dict[str, Any],
    *,
    level: str,
    event: str,
    message: str,
    graph: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "time": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "message": message,
        **_runtime_node_data(graph, node),
        "data": data or {},
    }
    events = _runtime_events(state)
    events.append(item)
    if len(events) > 100:
        del events[:-100]
    return item


def _log_runtime_event(item: dict[str, Any]) -> None:
    level = str(item.get("level") or "info").lower()
    log_method = logger.error if level == "error" else logger.warning if level == "warning" else logger.info
    log_method(
        str(item.get("message") or "Channel runtime event."),
        extra={"event": item.get("event") or "channel.runtime.event", "extra_data": item},
    )


def _set_variable(variables: dict[str, Any], name: str | None, value: Any) -> None:
    set_variable(variables, name, value)


def _set_function_result_variables(variables: dict[str, Any], result_name: str, value: Any, status_value: object) -> None:
    normalized_name = str(result_name or "").strip() or "apiResult"
    _set_variable(variables, normalized_name, value)
    _set_variable(variables, f"{normalized_name}.__status", str(status_value or ""))
    _set_variable(variables, "result", value)
    _set_variable(variables, "result.__status", str(status_value or ""))


def _get_variable(variables: dict[str, Any], name: str | None) -> Any:
    return get_variable(variables, name)


def _stringify_variable(value: Any, accessor: str | None = None) -> str:
    return stringify_variable(value, accessor)


def _variable_empty_debug_data(expression: str, variables: dict[str, Any]) -> dict[str, Any]:
    normalized = str(expression or "").strip().lstrip("$")
    dot_index = normalized.find(".")
    bracket_index = normalized.find("[")
    indexes = [index for index in (dot_index, bracket_index) if index >= 0]
    split_index = min(indexes) if indexes else -1
    root = normalized if split_index < 0 else normalized[:split_index]
    path = "" if split_index < 0 else normalized[split_index + (1 if normalized[split_index] == "." else 0) :]
    root_key = var_key(root) if root else ""
    root_value = variables.get(root_key, variables.get(root, ""))
    return {
        "expression": expression,
        "rootVariable": root_key,
        "path": path,
        "rootVariablePresent": bool(root and (root_key in variables or root in variables)),
        "rootValuePreview": _log_preview(root_value, 300) if root and (root_key in variables or root in variables) else "",
        "availableVariables": sorted(str(key) for key in variables.keys() if str(key).startswith("$"))[:30],
    }


def _render_text(text: str, variables: dict[str, Any], state: dict[str, Any] | None = None, graph: dict[str, Any] | None = None, node: dict[str, Any] | None = None) -> str:
    def on_empty(expression: str) -> None:
        if state is not None:
            item = _append_runtime_event(
                state,
                level="warning",
                event="channel.runtime.variable_empty",
                message="변수 치환 결과가 비어 있습니다.",
                graph=graph,
                node=node,
                data=_variable_empty_debug_data(expression, variables),
            )
            _log_runtime_event(item)

    return render_text(text, variables, on_empty)


def _evaluate_runtime_value(expression: str, variables: dict[str, Any]) -> Any:
    text = str(expression or "").strip()
    template_match = re.fullmatch(r"\{\{\s*([^}]+?)\s*\}\}", text)
    if template_match:
        return evaluate_expression(template_match.group(1), variables)
    if "(" in text and ")" in text:
        return evaluate_expression(text, variables)
    return _get_variable(variables, text)


def _normalize_entity_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _entity_candidate_values(row: dict[str, Any]) -> list[str]:
    values = [str(row.get("value") or "").strip()]
    values.extend(str(item or "").strip() for item in _as_list(row.get("details")))
    return [value for value in values if value]


def _extract_entity_value(document: dict[str, Any], extraction: dict[str, Any], message: str) -> dict[str, Any]:
    entity_id = str(extraction.get("entityId") or "").strip()
    entity_name = str(extraction.get("entityName") or "").strip().lstrip("@")
    message_normalized = _normalize_entity_text(message)
    entities = [entity for entity in _as_list(document.get("entities")) if isinstance(entity, dict)]
    entity = next(
        (
            item
            for item in entities
            if str(item.get("id") or "") == entity_id or str(item.get("name") or "") == entity_name
        ),
        None,
    )
    if entity is None:
        return {"text": message, "value": message, "target": message, "entity": entity_name or entity_id}

    for row in _as_list(entity.get("rows")):
        if not isinstance(row, dict):
            continue
        target = str(row.get("value") or "").strip()
        for candidate in _entity_candidate_values(row):
            if _normalize_entity_text(candidate) and _normalize_entity_text(candidate) in message_normalized:
                return {"text": candidate, "value": candidate, "target": target or candidate, "entity": str(entity.get("name") or entity_name)}
    return {"text": message, "value": message, "target": message, "entity": str(entity.get("name") or entity_name)}


def _json_loads_loose(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        repaired = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', value)
        if repaired == value:
            raise
        return json.loads(repaired)


def _parse_json_or_none(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _json_loads_loose(value)
    except json.JSONDecodeError:
        first_value = _extract_first_json_value(value)
        if first_value:
            try:
                return _json_loads_loose(first_value)
            except json.JSONDecodeError:
                return None
        return None


def _parse_rich_form_or_none(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return _json_loads_loose(text)
    except json.JSONDecodeError:
        try:
            return _json_loads_loose(f"[{text}]")
        except json.JSONDecodeError:
            recovered_components: list[Any] = []
            for source in _extract_json_values(text):
                parsed = _parse_json_or_none(source)
                if isinstance(parsed, list):
                    recovered_components.extend(item for item in parsed if isinstance(item, dict))
                elif isinstance(parsed, dict):
                    recovered_components.append(parsed)
            if recovered_components:
                return recovered_components
            return _parse_json_or_none(text)


def _dedupe_rich_form_components(components: list[Any]) -> list[Any]:
    deduped: list[Any] = []
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            continue
        key = json.dumps(component, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(component)
    return deduped


def _rich_form_source_from_messages(messages: list[str], fallback_source: str) -> tuple[Any, str]:
    components: list[Any] = []
    json_sources = [item for item in messages if item.startswith("{") or item.startswith("[")]
    for source in json_sources:
        parsed = _parse_rich_form_or_none(source)
        if isinstance(parsed, list):
            components.extend(item for item in parsed if isinstance(item, dict))
            continue
        if isinstance(parsed, dict):
            nested = parsed.get("richForm")
            if isinstance(nested, list):
                components.extend(item for item in nested if isinstance(item, dict))
            else:
                components.append(parsed)
    if components:
        components = _dedupe_rich_form_components(components)
        return components, json.dumps(components, ensure_ascii=False)
    form_source = json_sources[0] if json_sources else fallback_source
    return _parse_rich_form_or_none(form_source), form_source


def _rich_form_button_value(message: str) -> str | None:
    parsed = _parse_json_or_none(message)
    if not isinstance(parsed, dict):
        return None
    response = _as_dict(parsed.get("response"))
    value = response.get("buttonValue")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _talk_response_variable_value(config: dict[str, Any], message: str, effective_message: str) -> Any:
    message_type = str(config.get("messageType") or "text")
    if message_type in {"form", "form-a-card"}:
        parsed = _parse_json_or_none(message)
        if parsed is not None:
            return parsed
    return effective_message


def _store_response_variable_value(variables: dict[str, Any], variable_name: str, value: Any) -> tuple[list[str], dict[str, str]]:
    updated_variables: list[str] = []
    value_previews: dict[str, str] = {}
    for child_name, child_value in _flatten_function_value(variable_name, value):
        _set_variable(variables, child_name, child_value)
        variable_key = var_key(child_name)
        if variable_key not in value_previews:
            updated_variables.append(variable_key)
        value_previews[variable_key] = _log_preview(child_value, 300)
    if isinstance(value, dict) and "response" in value:
        for child_name, child_value in _flatten_function_value(f"{variable_name}.response", value.get("response")):
            _set_variable(variables, child_name, child_value)
            variable_key = var_key(child_name)
            if variable_key not in value_previews:
                updated_variables.append(variable_key)
            value_previews[variable_key] = _log_preview(child_value, 300)
    return updated_variables, value_previews


def _rich_form_button_options(value: Any) -> list[str]:
    options: list[str] = []
    for action in _rich_form_button_actions(value):
        option = str(action.get("messageText") or action.get("label") or "").strip()
        if option and option not in options:
            options.append(option)
    return options


def _single_select_options(config: dict[str, Any]) -> list[str]:
    options: list[str] = []
    for item in _as_list(config.get("messages")):
        for option in _rich_form_button_options(item):
            if option not in options:
                options.append(option)
        text = str(item or "").strip()
        if text and not text.startswith("{") and not text.startswith("[") and text not in options:
            options.append(text)
    return options


def _extract_first_json_value(value: str) -> str | None:
    values = _extract_json_values(value)
    return values[0] if values else None


def _extract_json_values(value: str) -> list[str]:
    values: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            if depth == 0:
                start = index
            depth += 1
        elif char in "}]":
            if depth == 0:
                start = -1
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                values.append(value[start : index + 1])
                start = -1
            if depth < 0:
                depth = 0
                start = -1
    return values


def _table_rows_from_variable(value: Any) -> list[dict[str, Any]]:
    parsed = _parse_json_or_none(value) if isinstance(value, str) else value
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    return []


def _channel_template_code(channel_type: str | None) -> str:
    normalized = (channel_type or "webchat").strip().lower()
    return {
        "webchat": "WEBCHAT",
        "kakao": "KAKAO",
        "ms-teams": "TEAMS",
    }.get(normalized, normalized.upper())


def _talk_config_for_channel(config: dict[str, Any], channel_code: str) -> dict[str, Any]:
    template = _as_dict(_as_dict(config.get("channelTemplates")).get(channel_code))
    if not template:
        return config
    return {**config, **template, "templateChannel": channel_code}

def _rich_form_component_can_submit(component: Any) -> bool:
    if not isinstance(component, dict):
        return False
    component_type = str(component.get("type") or "").strip().upper()
    if not component_type:
        return False
    if component_type in {"FORM_TITLE", "TEXT", "HR", "IMAGE", "FIELDS", "VIDEO"}:
        return False
    if component_type == "TABLE":
        return component.get("selectable") is not False
    return True


def _rich_form_output_can_submit(output: dict[str, Any]) -> bool:
    payload = _as_dict(output.get("payload"))
    form = payload.get("richForm")
    if form is None:
        form = _parse_rich_form_or_none(str(payload.get("rawJson") or ""))
    components: list[Any]
    if isinstance(form, list):
        components = form
    elif isinstance(form, dict) and isinstance(form.get("richForm"), list):
        components = form.get("richForm") or []
    elif isinstance(form, dict):
        components = [form]
    else:
        components = []
    return any(_rich_form_component_can_submit(component) for component in components)


def _adaptive_card_can_submit(value: Any) -> bool:
    if isinstance(value, list):
        return any(_adaptive_card_can_submit(item) for item in value)
    if not isinstance(value, dict):
        return False
    card_type = str(value.get("type") or "")
    if card_type == "Action.Submit" or card_type.startswith("Input."):
        return True
    return any(_adaptive_card_can_submit(item) for item in value.values())


def _runtime_output_can_submit(output: dict[str, Any] | None) -> bool:
    if not output:
        return False
    output_type = str(output.get("type") or "")
    if output_type == "form":
        return _rich_form_output_can_submit(output)
    if output_type == "form-a-card":
        payload = _as_dict(output.get("payload"))
        card = payload.get("adaptiveCard")
        if card is None:
            card = _parse_json_or_none(str(payload.get("rawJson") or ""))
        return _adaptive_card_can_submit(card)
    return bool(output.get("options"))


def _talk_should_wait(config: dict[str, Any], output: dict[str, Any] | None = None) -> bool:
    if str(config.get("messageType") or "text") == "dtmf":
        return True
    response_type = str(config.get("responseType") or "none")
    if response_type == "form-relay":
        return _runtime_output_can_submit(output)
    return response_type in {"single-select", "relay", "extract-entity"}


def _dtmf_settings(config: dict[str, Any]) -> dict[str, Any]:
    messages = _as_list(config.get("messages"))

    def number_at(index: int, default: int) -> int:
        value = _to_number(messages[index]) if len(messages) > index else None
        return max(1, int(value)) if value is not None else default

    max_length = number_at(0, 1)
    min_length = min(max_length, number_at(1, 1))
    end_character = str(messages[2] or "#").strip() if len(messages) > 2 else "#"
    if end_character not in {"#", "*"}:
        end_character = "#"
    return {
        "minLength": min_length,
        "maxLength": max_length,
        "endCharacter": end_character,
        "firstInputTimeoutMs": number_at(3, 10) * 100,
        "overallInputTimeoutMs": number_at(4, 10) * 100,
    }


def _normalized_dtmf_input(value: str, settings: dict[str, Any]) -> str | None:
    normalized = value.strip()
    end_character = str(settings.get("endCharacter") or "#")
    if normalized.endswith(end_character):
        normalized = normalized[: -len(end_character)]
    if not normalized or not normalized.isdecimal():
        return None
    min_length = int(settings.get("minLength") or 1)
    max_length = int(settings.get("maxLength") or 1)
    if not min_length <= len(normalized) <= max_length:
        return None
    return normalized


def _talk_output(
    node: dict[str, Any],
    variables: dict[str, Any],
    default_messages: dict[str, str] | None = None,
    channel_code: str = "WEBCHAT",
    state: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    default_messages = default_messages or {}
    config = _talk_config_for_channel(_as_dict(node.get("config")), channel_code)
    message_type = str(config.get("messageType") or "text")
    response_type = str(config.get("responseType") or "none")
    text = _render_text(_first_text(config.get("basicMessages")), variables, state, graph, node)
    messages = [_render_text(str(item or ""), variables, state, graph, node).strip() for item in _as_list(config.get("messages"))]
    options = [item for item in messages if item]

    if message_type == "button":
        if not text and not options:
            return None
        return {"type": "button", "text": text or _runtime_message(default_messages, "generic_select", DEFAULT_MESSAGE_FALLBACKS["generic_select"]), "options": options, "payload": {"sourceTalkNodeId": str(node.get("id") or "")}}

    if message_type == "card":
        title = options[0] if len(options) > 0 else ""
        image_url = options[1] if len(options) > 1 else ""
        description = options[2] if len(options) > 2 else ""
        if not text and not title and not image_url and not description:
            return None
        return {
            "type": "card",
            "text": text or title or "카드",
            "options": [],
            "payload": {
                "card": {
                    "title": title,
                    "imageUrl": image_url,
                    "description": description,
                },
                "sourceTalkNodeId": str(node.get("id") or ""),
            },
        }

    if message_type == "link-button":
        link_items = [
            {
                "label": str(item.get("label") or "").strip(),
                "url": str(item.get("url") or "").strip(),
            }
            for item in _as_list(config.get("linkButtonItems"))
            if isinstance(item, dict) and str(item.get("label") or "").strip() and str(item.get("url") or "").strip()
        ]
        if not text and not link_items:
            return None
        return {
            "type": "link-button",
            "text": text or _runtime_message(default_messages, "generic_select", DEFAULT_MESSAGE_FALLBACKS["generic_select"]),
            "options": link_items,
            "payload": {"sourceTalkNodeId": str(node.get("id") or "")},
        }

    if message_type == "dtmf":
        settings = _dtmf_settings(config)
        return {
            "type": "dtmf",
            "text": text or "번호를 입력하세요.",
            "options": [],
            "payload": {
                "dtmf": settings,
                "sourceTalkNodeId": str(node.get("id") or ""),
            },
        }

    if message_type == "table":
        item_id = str(config.get("tableVariableItemId") or "").strip()
        item_values = _as_dict(variables.get("__items"))
        rows = _table_rows_from_variable(item_values.get(item_id))
        columns = [str(item.get("column") or item.get("value") or "").strip() for item in _as_list(config.get("tableColumnMappings")) if isinstance(item, dict)]
        columns = [column for column in columns if column]
        if not columns and rows:
            columns = list(rows[0].keys())
        return {
            "type": "table",
            "text": text or _runtime_message(default_messages, "table_select", DEFAULT_MESSAGE_FALLBACKS["table_select"]),
            "options": [],
            "payload": {"columns": columns, "rows": rows, "keyColumn": str(config.get("tableKeyColumn") or ""), "selectable": response_type == "single-select", "sourceTalkNodeId": str(node.get("id") or "")},
        }

    if message_type == "carousel":
        title = options[0] if len(options) > 0 else ""
        image_url = options[1] if len(options) > 1 else ""
        item_title = options[2] if len(options) > 2 else ""
        item_contents = options[3] if len(options) > 3 else ""
        item_button_label = options[4] if len(options) > 4 else ""
        item_button_value = options[5] if len(options) > 5 else ""
        button_type = options[6] if len(options) > 6 else "Hidden Button"
        bottom_button_label = options[7] if len(options) > 7 else ""
        bottom_button_value = options[8] if len(options) > 8 else ""
        if not text and not title and not image_url and not item_title and not item_contents:
            return None
        return {
            "type": "carousel",
            "text": text or title or item_title or "Carousel",
            "options": [],
            "payload": {
                "carousel": {
                    "title": title,
                    "imageUrl": image_url,
                    "itemTitle": item_title,
                    "itemContents": item_contents,
                    "itemButtonLabel": item_button_label,
                    "itemButtonValue": item_button_value,
                    "buttonType": button_type,
                    "bottomButtonLabel": bottom_button_label,
                    "bottomButtonValue": bottom_button_value,
                },
                "sourceTalkNodeId": str(node.get("id") or ""),
            },
        }

    if message_type == "form-a-card":
        card_source = next((item for item in messages if item.startswith("{") or item.startswith("[")), "")
        card = _parse_json_or_none(card_source)
        return {
            "type": "form-a-card",
            "text": text or "Adaptive Card",
            "options": [],
            "payload": {"adaptiveCard": card, "rawJson": card_source, "sourceTalkNodeId": str(node.get("id") or "")},
        }

    if not text and not options:
        return None
    if message_type == "form":
        fallback_source = _fallback_rich_form_source(text or str(node.get("title") or "")) or text
        form, form_source = _rich_form_source_from_messages(messages, fallback_source)
        return {
            "type": "form",
            "text": text or "RichForm",
            "options": [],
            "payload": {"richForm": form, "rawJson": form_source, "sourceTalkNodeId": str(node.get("id") or "")},
        }
    return {"type": "text", "text": text or options[0], "options": [], "payload": {"sourceTalkNodeId": str(node.get("id") or "")}}



def _fallback_rich_form_source(label: str) -> str:
    normalized = re.sub(r"[\s_-]+", "_", label.strip().upper())
    if normalized == "INPUT":
        return json.dumps([{"type": "INPUT", "key": "name", "title": "이름", "required": True}], ensure_ascii=False)
    if normalized in {"FORM_TITLE", "FORMTITLE"}:
        return json.dumps([
            {"type": "FORM_TITLE", "title": "INPUT 컴포넌트 테스트", "divider": True},
            {"type": "TEXT", "text": "텍스트1"},
            {"type": "HR"},
            {"type": "TEXT", "text": "텍스트2"},
        ], ensure_ascii=False)
    return ""
def _script_assignments(code: str) -> dict[str, Any]:
    assignments: dict[str, Any] = {}
    for match in re.finditer(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([\s\S]*?);", code):
        name = match.group(1)
        value = _parse_json_or_none(match.group(2).strip())
        if value is not None:
            assignments[name] = value
    return assignments


def _run_script_node(node: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    config = _as_dict(node.get("config"))
    assignments = _script_assignments(str(config.get("code") or ""))
    item_values = _as_dict(variables.get("__items"))
    variables["__items"] = item_values
    node_id = str(node.get("id") or "")
    updated_variables: list[str] = []
    missing_script_variables: list[str] = []
    for item in _as_list(config.get("returnVariables")):
        if not isinstance(item, dict):
            continue
        variable_name = str(item.get("variableName") or "").strip()
        script_variable_name = str(item.get("scriptVariableName") or "").strip()
        if script_variable_name not in assignments:
            missing_script_variables.append(script_variable_name)
        value = assignments.get(script_variable_name, "")
        _set_variable(variables, variable_name, value)
        if variable_name:
            updated_variables.append(var_key(variable_name))
        return_id = str(item.get("id") or "").strip()
        if return_id:
            item_values[return_id] = value
            if node_id:
                item_values[f"{node_id}-{return_id}"] = value
    return {
        "updatedVariables": updated_variables,
        "missingScriptVariables": [item for item in missing_script_variables if item],
        "assignmentNames": sorted(assignments.keys()),
    }

def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _condition_matches(operator: str, value: Any, compare_value: str) -> bool:
    value_text = str(value if value is not None else "")
    if operator == "exists":
        return value not in (None, "")
    if operator == "not-exists":
        return value in (None, "")
    if operator == "equals":
        return value_text == compare_value
    if operator == "not-equals":
        return value_text != compare_value
    if operator == "contains":
        return compare_value in value_text
    if operator in {"greater-than", "greater-or-equal", "less-than", "less-or-equal"}:
        left = _to_number(value)
        right = _to_number(compare_value)
        if left is None or right is None:
            return False
        if operator == "greater-than":
            return left > right
        if operator == "greater-or-equal":
            return left >= right
        if operator == "less-than":
            return left < right
        return left <= right
    if operator == "regex":
        try:
            return re.search(compare_value, value_text) is not None
        except re.error:
            return False
    return False


def _condition_next_node_id(graph: dict[str, Any], node: dict[str, Any], variables: dict[str, Any], state: dict[str, Any]) -> str | None:
    config = _as_dict(node.get("config"))
    variable_name = str(config.get("variableName") or "")
    value = _evaluate_runtime_value(variable_name, variables)
    branches = [branch for branch in _as_list(config.get("branches")) if isinstance(branch, dict)]
    else_branch = None
    selected_branch = None
    evaluated_branches: list[dict[str, Any]] = []
    for branch in branches:
        operator = str(branch.get("operator") or "")
        if operator == "else":
            else_branch = branch
            continue
        compare_value = _render_text(str(branch.get("compareValue") or ""), variables, state, graph, node)
        matched = _condition_matches(operator, value, compare_value)
        evaluated_branches.append(
            {
                "branchId": str(branch.get("id") or ""),
                "branchLabel": str(branch.get("label") or ""),
                "operator": operator,
                "compareValue": _log_preview(compare_value, 300),
                "matched": matched,
            }
        )
        if matched:
            selected_branch = branch
            break
    branch = selected_branch or else_branch
    selected_by_else = selected_branch is None and else_branch is not None
    if not branch:
        item = _append_runtime_event(
            state,
            level="error",
            event="channel.runtime.condition_no_else",
            message="Condition 카드에 '그 외의 경우' 분기가 없어 대화를 계속할 수 없습니다.",
            graph=graph,
            node=node,
            data={
                "variableName": variable_name,
                "value": _log_preview(value, 300),
                "evaluatedBranches": evaluated_branches,
            },
        )
        _log_runtime_event(item)
        return None
    next_node_id = _next_node_id(graph, str(node.get("id") or ""), f"branch:{branch.get('id')}")
    if not next_node_id:
        item = _append_runtime_event(
            state,
            level="error",
            event="channel.runtime.condition_target_missing",
            message="Condition 분기 연결 대상이 없어 대화를 계속할 수 없습니다.",
            graph=graph,
            node=node,
            data={
                "variableName": variable_name,
                "value": _log_preview(value, 300),
                "branchId": str(branch.get("id") or ""),
                "branchLabel": str(branch.get("label") or ""),
                "operator": str(branch.get("operator") or ""),
                "selectedByElse": selected_by_else,
                "evaluatedBranches": evaluated_branches,
            },
        )
        _log_runtime_event(item)
    else:
        item = _append_runtime_event(
            state,
            level="info",
            event="channel.runtime.condition_selected",
            message="Condition 분기 조건으로 이동했습니다.",
            graph=graph,
            node=node,
            data={
                "variableName": variable_name,
                "value": _log_preview(value, 300),
                "branchId": str(branch.get("id") or ""),
                "branchLabel": str(branch.get("label") or ""),
                "operator": str(branch.get("operator") or ""),
                "selectedByElse": selected_by_else,
                "evaluatedBranches": evaluated_branches,
                "targetNodeId": next_node_id,
            },
        )
        _log_runtime_event(item)
    return next_node_id


def _runtime_message(default_messages: dict[str, str], key: str, fallback: str) -> str:
    return default_messages.get(key) or fallback


def _runtime_text_output(default_messages: dict[str, str], key: str, fallback: str) -> dict[str, Any]:
    return {"type": "text", "text": _runtime_message(default_messages, key, fallback), "options": []}


def _runtime_flow_error_output(default_messages: dict[str, str]) -> dict[str, Any]:
    return _runtime_text_output(default_messages, "runtime_flow_error", DEFAULT_MESSAGE_FALLBACKS["runtime_flow_error"])


def _runtime_system_error_output(default_messages: dict[str, str]) -> dict[str, Any]:
    return _runtime_text_output(default_messages, "system_error", DEFAULT_MESSAGE_FALLBACKS["system_error"])


def _run_runtime(
    document: dict[str, Any],
    state: dict[str, Any],
    start_node_id: str | None,
    default_messages: dict[str, str] | None = None,
    channel_code: str = "WEBCHAT",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    default_messages = default_messages or {}
    outputs: list[dict[str, Any]] = []
    variables = _as_dict(state.get("variables"))
    state["variables"] = variables
    graph = _find_graph(document, graph_id=str(state.get("graphId") or ""), dialog_id=str(state.get("dialogId") or ""))
    node_id = start_node_id

    for _ in range(40):
        if graph is None:
            item = _append_runtime_event(
                state,
                level="error",
                event="channel.runtime.graph_missing",
                message="실행할 대화 흐름 그래프를 찾지 못했습니다.",
                data={"graphId": str(state.get("graphId") or ""), "dialogId": str(state.get("dialogId") or "")},
            )
            _log_runtime_event(item)
            outputs.append(_runtime_flow_error_output(default_messages))
            state["waitingNodeId"] = ""
            state["currentNodeId"] = ""
            state["dialogEnded"] = True
            return outputs, state
        if not node_id:
            state["waitingNodeId"] = ""
            state["currentNodeId"] = ""
            return outputs, state
        node = _find_node(graph, node_id)
        if node is None:
            item = _append_runtime_event(
                state,
                level="error",
                event="channel.runtime.node_missing",
                message="실행할 대화 카드를 찾지 못했습니다.",
                graph=graph,
                data={"nodeId": node_id},
            )
            _log_runtime_event(item)
            outputs.append(_runtime_flow_error_output(default_messages))
            state["waitingNodeId"] = ""
            state["currentNodeId"] = ""
            state["dialogEnded"] = True
            return outputs, state

        state["graphId"] = str(graph.get("id") or "")
        state["dialogId"] = str(graph.get("dialogId") or "")
        runtime_dialog = _runtime_dialog(document, state)
        state["__runtimeTransitionLocked"] = bool(runtime_dialog is not None and runtime_dialog.get("transitionLocked") is True)
        state["__runtimeReturnBlocked"] = bool(runtime_dialog is not None and runtime_dialog.get("returnBlocked") is True)
        state["currentNodeId"] = str(node.get("id") or "")
        kind = str(node.get("kind") or "")
        config = _as_dict(node.get("config"))

        if kind == "talk":
            output = _talk_output(node, variables, default_messages, channel_code, state, graph)
            if output:
                outputs.append(output)
            channel_config = _talk_config_for_channel(config, channel_code)
            next_talk_node_id = _next_node_id(graph, str(node.get("id") or ""))
            waits_for_response = _talk_should_wait(channel_config, output)
            item = _append_runtime_event(
                state,
                level="info" if output else "warning",
                event="channel.runtime.talk_completed",
                message="Talk 카드를 실행했습니다.",
                graph=graph,
                node=node,
                data={
                    "messageType": str(channel_config.get("messageType") or "text"),
                    "responseType": str(channel_config.get("responseType") or "none"),
                    "outputType": str(_as_dict(output).get("type") or ""),
                    "hasOutput": output is not None,
                    "waitsForResponse": waits_for_response,
                    "nextNodeId": next_talk_node_id,
                },
            )
            _log_runtime_event(item)
            if waits_for_response:
                state["waitingNodeId"] = str(node.get("id") or "")
                return outputs, state
            node_id = next_talk_node_id
            continue

        if kind == "condition":
            node_id = _condition_next_node_id(graph, node, variables, state)
            if not node_id:
                outputs.append(_runtime_flow_error_output(default_messages))
                state["waitingNodeId"] = ""
                state["currentNodeId"] = ""
                state["dialogEnded"] = True
                return outputs, state
            continue

        if kind == "jump":
            if str(config.get("targetType") or "dialog") == "card":
                node_id = str(config.get("targetCardId") or "")
                if not _find_node(graph, node_id):
                    item = _append_runtime_event(
                        state,
                        level="error",
                        event="channel.runtime.jump_card_missing",
                        message="Jump 카드가 이동할 카드를 찾지 못했습니다.",
                        graph=graph,
                        node=node,
                        data={"targetCardId": str(config.get("targetCardId") or "")},
                    )
                    _log_runtime_event(item)
                    outputs.append(
                        _runtime_text_output(
                            default_messages,
                            "runtime_module_not_found",
                            DEFAULT_MESSAGE_FALLBACKS["runtime_module_not_found"],
                        )
                    )
                    state["waitingNodeId"] = ""
                    state["dialogEnded"] = True
                    return outputs, state
                item = _append_runtime_event(
                    state,
                    level="info",
                    event="channel.runtime.jump_card_selected",
                    message="Jump 카드가 같은 대화의 카드로 이동했습니다.",
                    graph=graph,
                    node=node,
                    data={"targetCardId": node_id},
                )
                _log_runtime_event(item)
                continue
            target_graph = _find_graph(
                document,
                dialog_id=str(config.get("targetDialogId") or ""),
                name=str(config.get("targetDialogName") or ""),
            )
            if target_graph is None:
                item = _append_runtime_event(
                    state,
                    level="error",
                    event="channel.runtime.jump_dialog_missing",
                    message="Jump 카드가 이동할 의도/모듈을 찾지 못했습니다.",
                    graph=graph,
                    node=node,
                    data={"targetDialogId": str(config.get("targetDialogId") or ""), "targetDialogName": str(config.get("targetDialogName") or "")},
                )
                _log_runtime_event(item)
                outputs.append(
                    _runtime_text_output(
                        default_messages,
                        "runtime_module_not_found",
                        "연결할 대화 모듈을 찾지 못했습니다.",
                    )
                )
                state["waitingNodeId"] = ""
                state["dialogEnded"] = True
                return outputs, state
            item = _append_runtime_event(
                state,
                level="info",
                event="channel.runtime.jump_dialog_selected",
                message="Jump 카드가 의도/모듈로 이동했습니다.",
                graph=graph,
                node=node,
                data={
                    "targetDialogId": str(target_graph.get("dialogId") or ""),
                    "targetGraphId": str(target_graph.get("id") or ""),
                    "targetGraphName": str(target_graph.get("name") or ""),
                },
            )
            _log_runtime_event(item)
            graph = target_graph
            target_dialog = next(
                (
                    dialog
                    for dialog in _safe_dialogs(document)
                    if str(dialog.get("id") or "").strip() == str(target_graph.get("dialogId") or "").strip()
                ),
                None,
            )
            state["__runtimeTransitionLocked"] = state.get("__runtimeTransitionLocked") is True or bool(
                target_dialog is not None and target_dialog.get("transitionLocked") is True
            )
            state["__runtimeReturnBlocked"] = bool(target_dialog is not None and target_dialog.get("returnBlocked") is True)
            node_id = _first_runtime_node_id(graph)
            continue

        if kind == "variable":
            item_values = _as_dict(variables.get("__items"))
            variables["__items"] = item_values
            updated_variables: list[str] = []
            updated_items: list[str] = []
            value_previews: dict[str, str] = {}
            for item in _as_list(config.get("items")):
                if isinstance(item, dict):
                    value = item.get("value")
                    if isinstance(value, str):
                        value = _render_text(value, variables, state, graph, node)
                    variable_name = str(item.get("variableName") or "")
                    _set_variable(variables, variable_name, value)
                    if variable_name.strip():
                        variable_key = var_key(variable_name)
                        updated_variables.append(variable_key)
                        value_previews[variable_key] = _log_preview(value, 300)
                    item_id = str(item.get("id") or "").strip()
                    if item_id:
                        item_values[item_id] = value
                        updated_items.append(item_id)
                        value_previews[item_id] = _log_preview(value, 300)
            item = _append_runtime_event(
                state,
                level="info",
                event="channel.runtime.variable_completed",
                message="Variable 카드를 실행했습니다.",
                graph=graph,
                node=node,
                data={"updatedVariables": updated_variables, "updatedItems": updated_items, "valuePreviews": value_previews},
            )
            _log_runtime_event(item)
            node_id = _next_node_id(graph, str(node.get("id") or ""))
            continue

        if kind == "script":
            result = _run_script_node(node, variables)
            item = _append_runtime_event(
                state,
                level="warning" if result["missingScriptVariables"] else "info",
                event="channel.runtime.script_completed",
                message="Script 카드를 실행했습니다.",
                graph=graph,
                node=node,
                data=result,
            )
            _log_runtime_event(item)
            node_id = _next_node_id(graph, str(node.get("id") or ""))
            continue

        if kind == "function":
            ok = _execute_function_node(document, node, variables, state, graph)
            if ok:
                item = _append_runtime_event(
                    state,
                    level="info",
                    event="channel.runtime.function_completed",
                    message="Function 카드를 실행했습니다.",
                    graph=graph,
                    node=node,
                    data=_as_dict(variables.get("__lastFunctionResult")),
                )
                _log_runtime_event(item)
                node_id = _next_node_id(graph, str(node.get("id") or ""), "next")
                continue
            exception_node_id = _next_node_id(graph, str(node.get("id") or ""), "exception")
            if exception_node_id:
                item = _append_runtime_event(
                    state,
                    level="warning",
                    event="channel.runtime.function_exception_flow",
                    message="Function 실행 실패 후 예외 흐름으로 이동했습니다.",
                    graph=graph,
                    node=node,
                    data=_as_dict(variables.get("__lastFunctionResult")),
                )
                _log_runtime_event(item)
                node_id = exception_node_id
                continue
            item = _append_runtime_event(
                state,
                level="error",
                event="channel.runtime.function_failed",
                message="Function 실행 실패 후 예외 흐름이 없어 대화를 종료합니다.",
                graph=graph,
                node=node,
                data=_as_dict(variables.get("__lastFunctionResult")),
            )
            _log_runtime_event(item)
            outputs.append(_runtime_system_error_output(default_messages))
            state["waitingNodeId"] = ""
            state["currentNodeId"] = ""
            state["dialogEnded"] = True
            return outputs, state

        if kind == "end":
            message = str(config.get("message") or "").strip()
            if message:
                outputs.append({"type": "text", "text": _render_text(message, variables, state, graph, node), "options": []})
            end_immediately = config.get("endSessionImmediately") is True
            before_session_end_module = str(state.get("__beforeSessionEndModule") or "").strip()
            if end_immediately and before_session_end_module and state.get("__beforeSessionEndModuleRan") is not True:
                module_graph = _graph_for_module_name(document, before_session_end_module)
                if module_graph is None:
                    item = _append_runtime_event(
                        state,
                        level="warning",
                        event="channel.runtime.before_session_end_module_missing",
                        message="Session End 전 실행할 모듈을 찾지 못했습니다.",
                        graph=graph,
                        node=node,
                        data={"moduleName": before_session_end_module},
                    )
                    _log_runtime_event(item)
                else:
                    item = _append_runtime_event(
                        state,
                        level="info",
                        event="channel.runtime.before_session_end_module_start",
                        message="Session End 전 실행할 모듈을 실행합니다.",
                        graph=graph,
                        node=node,
                        data={"moduleName": before_session_end_module, "moduleGraphId": str(module_graph.get("id") or "")},
                    )
                    _log_runtime_event(item)
                    state["__beforeSessionEndModuleRan"] = True
                    module_state = {
                        **state,
                        "graphId": str(module_graph.get("id") or ""),
                        "dialogId": str(module_graph.get("dialogId") or ""),
                        "currentNodeId": str(_first_runtime_node_id(module_graph) or ""),
                        "waitingNodeId": "",
                        "variables": variables,
                    }
                    module_outputs, module_state = _run_runtime(
                        document,
                        module_state,
                        str(module_state.get("currentNodeId") or ""),
                        default_messages,
                        channel_code,
                    )
                    outputs.extend(module_outputs)
                    state.update(module_state)
                    variables = _as_dict(state.get("variables"))
                    state["variables"] = variables
                    if state.get("waitingNodeId"):
                        return outputs, state
            if not end_immediately and _restore_runtime_return(state):
                item = _append_runtime_event(
                    state,
                    level="info",
                    event="channel.runtime.return_restored",
                    message="이전 대화 흐름으로 복귀했습니다.",
                    graph=graph,
                    node=node,
                )
                _log_runtime_event(item)
                resume_node_id = str(state.get("waitingNodeId") or state.get("currentNodeId") or "").strip()
                if resume_node_id:
                    resumed_outputs, resumed_state = _run_runtime(
                        document,
                        state,
                        resume_node_id,
                        default_messages,
                        channel_code,
                    )
                    outputs.extend(resumed_outputs)
                    return outputs, resumed_state
                return outputs, state
            item = _append_runtime_event(
                state,
                level="info",
                event="channel.runtime.end",
                message="End 카드에 도달했습니다.",
                graph=graph,
                node=node,
                data={"endSessionImmediately": end_immediately},
            )
            _log_runtime_event(item)
            if end_immediately:
                capture_secondary_terminal_result(state)
            apply_end_card_state(state, end_session_immediately=end_immediately)
            return outputs, state

        next_node_id = _next_node_id(graph, str(node.get("id") or ""))
        if not next_node_id:
            item = _append_runtime_event(
                state,
                level="warning",
                event="channel.runtime.next_node_missing",
                message="다음 연결 카드가 없어 대화를 종료합니다.",
                graph=graph,
                node=node,
            )
            _log_runtime_event(item)
            state["waitingNodeId"] = ""
            state["currentNodeId"] = ""
            state["dialogEnded"] = True
            return outputs, state
        node_id = next_node_id

    item = _append_runtime_event(
        state,
        level="error",
        event="channel.runtime.flow_limit_exceeded",
        message="대화 흐름 실행 한도를 초과했습니다.",
        graph=graph,
        data={"limit": 40},
    )
    _log_runtime_event(item)
    outputs.append(
        _runtime_text_output(
            default_messages,
            "runtime_flow_limit",
            DEFAULT_MESSAGE_FALLBACKS["runtime_flow_limit"],
        )
    )
    state["waitingNodeId"] = ""
    state["currentNodeId"] = ""
    state["dialogEnded"] = True
    return outputs, state


def _document_api_assets(document: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("apis", "api_assets", "apiAssets"):
        items = _as_list(document.get(key))
        if items:
            return [item for item in items if isinstance(item, dict)]
    return []


def _join_api_url(base_url: str, method_url: str, path_values: dict[str, str]) -> str:
    base = base_url.rstrip("/")
    path = method_url if method_url.startswith("/") else f"/{method_url}"
    for key, value in path_values.items():
        path = path.replace(f"{{{key}}}", str(value))
    return f"{base}{path}"


def _function_output_value(source: Any, path: str) -> Any:
    normalized = str(path or "root").strip()
    if not normalized or normalized == "root":
        return source
    if normalized.startswith("root."):
        normalized = normalized[5:]
    elif normalized.startswith("root["):
        normalized = normalized[4:]
    return object_path_value(source, normalized)


def _flatten_function_value(variable_name: str, value: Any, depth: int = 0) -> list[tuple[str, Any]]:
    entries = [(variable_name, value)]
    if depth >= 12 or value is None:
        return entries
    if isinstance(value, list):
        for index, item in enumerate(value):
            entries.extend(_flatten_function_value(f"{variable_name}.{index}", item, depth + 1))
    elif isinstance(value, dict):
        for key, child in value.items():
            entries.extend(_flatten_function_value(f"{variable_name}.{key}", child, depth + 1))
    return entries


_SENSITIVE_LOG_KEY_PARTS = ("token", "secret", "password", "authorization", "api_key", "apikey", "credential", "cookie")


def _mask_sensitive_text(value: str) -> str:
    masked = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", value)
    masked = re.sub(r'(?i)(["\']?(?:token|secret|password|authorization|api[_-]?key|credential|cookie)["\']?\s*[:=]\s*["\']?)[^\s,;"\']+', r"\1***", masked)
    return masked


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if any(part in str(key).lower() for part in _SENSITIVE_LOG_KEY_PARTS) else _redact_log_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    if isinstance(value, str):
        return _mask_sensitive_text(value)
    return value

def _log_preview(value: Any, max_length: int = 1200) -> str:
    try:
        redacted_value = _redact_log_value(value)
        text = json.dumps(redacted_value, ensure_ascii=False, default=str) if isinstance(redacted_value, (dict, list)) else str(redacted_value or "")
    except Exception:
        text = str(value or "")
    if len(text) <= max_length:
        return text
    return f"{text[:max_length]}..."


def _function_log_data(
    *,
    node: dict[str, Any],
    api: dict[str, Any] | None,
    method: dict[str, Any] | None,
    api_id: str,
    method_id: str,
    url: str | None = None,
    request_data: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node_id": node.get("id"),
        "node_title": node.get("title"),
        "api_id": api_id,
        "api_name": _as_dict(api).get("name"),
        "method_id": method_id,
        "method_name": _as_dict(method).get("name"),
        "http_method": _as_dict(method).get("httpMethod"),
    }
    if url:
        payload["url"] = url
    if request_data is not None:
        payload["request"] = request_data
    if result is not None:
        payload.update(
            {
                "ok": result.get("ok"),
                "status": result.get("status"),
                "status_text": result.get("statusText"),
                "elapsed_ms": result.get("elapsedMs"),
                "message": result.get("message"),
                "response_preview": _log_preview(result.get("body") if result.get("body") is not None else result.get("text")),
            }
        )
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error_message"] = str(error)
    return payload


def _execute_function_node(document: dict[str, Any], node: dict[str, Any], variables: dict[str, Any], state: dict[str, Any] | None = None, graph: dict[str, Any] | None = None) -> bool:
    config = _as_dict(node.get("config"))
    api_id = str(config.get("apiId") or "")
    method_id = str(config.get("methodId") or "")
    api = next((item for item in _document_api_assets(document) if str(item.get("id") or "") == api_id), None)
    method = next((item for item in _as_list(_as_dict(api).get("methods")) if isinstance(item, dict) and str(item.get("id") or "") == method_id), None)
    if not api or not method:
        message = "Function 카드의 API 또는 Method가 설정되어 있지 않습니다."
        variables["__lastFunctionResult"] = {
            "ok": False,
            "status": 0,
            "message": message,
            "apiId": api_id,
            "apiName": _as_dict(api).get("name"),
            "methodId": method_id,
            "methodName": _as_dict(method).get("name"),
            "httpMethod": _as_dict(method).get("httpMethod"),
        }
        logger.warning(
            "Channel function API mapping missing.",
            extra={"event": "channel.function.missing", "extra_data": _function_log_data(node=node, api=api, method=method, api_id=api_id, method_id=method_id)},
        )
        return False
    mappings = [item for item in _as_list(config.get("parameterMappings")) if isinstance(item, dict)]
    parameters = [item for item in _as_list(method.get("parameters")) if isinstance(item, dict)]
    parameter_values: dict[str, str] = {}
    for parameter in parameters:
        name = str(parameter.get("name") or "")
        mapping = next((item for item in mappings if str(item.get("parameterId") or "") == str(parameter.get("id") or "") or str(item.get("name") or "") == name), None)
        raw_value = str(_as_dict(mapping).get("value") if mapping else parameter.get("defaultValue") or "")
        parameter_values[name] = _render_text(raw_value, variables, state, graph, node)
    path_values = {str(parameter.get("name") or ""): parameter_values.get(str(parameter.get("name") or ""), "") for parameter in parameters if str(parameter.get("location") or "") == "path"}
    url = _join_api_url(str(api.get("baseUrl") or ""), str(method.get("methodUrl") or ""), path_values)
    query_params = {str(parameter.get("name") or ""): parameter_values.get(str(parameter.get("name") or ""), "") for parameter in parameters if str(parameter.get("location") or "") == "query" and parameter_values.get(str(parameter.get("name") or ""), "").strip()}
    if query_params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(query_params)}"
    request_summary = {
        "parameterValues": parameter_values,
        "pathParams": path_values,
        "queryParams": query_params,
    }
    started_at = time.time()
    try:
        request = UrlRequest(url, method=str(method.get("httpMethod") or "GET").upper())
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            response_body = _parse_json_or_none(response_text) if response_text else None
            if response_body is None and response_text:
                response_body = response_text
            result = {"ok": 200 <= response.status < 300, "status": response.status, "statusText": getattr(response, "reason", ""), "elapsedMs": int((time.time() - started_at) * 1000), "body": response_body, "text": response_text}
    except HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        response_body = _parse_json_or_none(response_text) if response_text else None
        if response_body is None and response_text:
            response_body = response_text
        result = {"ok": False, "status": error.code, "statusText": getattr(error, "reason", "Function Execute Error"), "elapsedMs": int((time.time() - started_at) * 1000), "body": response_body, "text": response_text, "message": str(error)}
    except Exception as error:
        result = {"ok": False, "status": 500, "statusText": "Function Execute Error", "elapsedMs": int((time.time() - started_at) * 1000), "body": None, "text": "", "message": str(error)}
        logger.exception(
            "Channel function API call failed.",
            extra={"event": "channel.function.exception", "extra_data": _function_log_data(node=node, api=api, method=method, api_id=api_id, method_id=method_id, url=url, request_data=request_summary, result=result, error=error)},
        )
    else:
        log_method = logger.info if result.get("ok") else logger.warning
        log_method(
            "Channel function API call completed.",
            extra={"event": "channel.function.completed", "extra_data": _function_log_data(node=node, api=api, method=method, api_id=api_id, method_id=method_id, url=url, request_data=request_summary, result=result)},
        )
    if not result.get("ok") and result.get("status") != 500:
        logger.warning(
            "Channel function API call returned an error status.",
            extra={"event": "channel.function.error_status", "extra_data": _function_log_data(node=node, api=api, method=method, api_id=api_id, method_id=method_id, url=url, request_data=request_summary, result=result)},
        )
    response_value = result.get("body") if result.get("body") is not None else result.get("text")
    selected_mappings = [mapping for mapping in _as_list(config.get("outputMappings")) if isinstance(mapping, dict) and str(mapping.get("variableName") or "").strip()]
    variables["__lastFunctionResult"] = {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "statusText": result.get("statusText"),
        "elapsedMs": result.get("elapsedMs"),
        "message": result.get("message"),
        "apiId": api_id,
        "apiName": _as_dict(api).get("name"),
        "methodId": method_id,
        "methodName": _as_dict(method).get("name"),
        "httpMethod": _as_dict(method).get("httpMethod"),
        "url": url,
        "request": request_summary,
        "responsePreview": _log_preview(response_value),
        "responseType": "empty" if response_value is None else ("json" if isinstance(response_value, (dict, list)) else "text"),
        "outputMappingCount": len(selected_mappings),
    }
    updated_variables: list[str] = []
    value_previews: dict[str, str] = {}
    if selected_mappings:
        for mapping in selected_mappings:
            variable_name = str(mapping.get("variableName") or "").strip()
            value = _function_output_value(result, str(mapping.get("path") or "root"))
            if value is None:
                value = _function_output_value(result.get("body"), str(mapping.get("path") or "root"))
            for child_name, child_value in _flatten_function_value(variable_name, value):
                _set_variable(variables, child_name, child_value)
                variable_key = var_key(child_name)
                updated_variables.append(variable_key)
                value_previews[variable_key] = _log_preview(child_value, 300)
    else:
        result_name = str(config.get("resultVariableName") or "apiResult").strip()
        result_value = result.get("body") if result.get("body") is not None else result.get("text", "")
        _set_function_result_variables(variables, result_name, result_value, result.get("status"))
        for variable_key, variable_value in (
            (var_key(result_name), result_value),
            (f"{var_key(result_name)}.__status", str(result.get("status") or "")),
            ("$result", result_value),
            ("$result.__status", str(result.get("status") or "")),
        ):
            updated_variables.append(variable_key)
            value_previews[variable_key] = _log_preview(variable_value, 300)
    result_name = str(config.get("resultVariableName") or "apiResult").strip()
    if result_name and selected_mappings:
        result_value = result.get("body") if result.get("body") is not None else result.get("text", "")
        status_value = str(result.get("status") or "")
        _set_variable(variables, "result", result_value)
        _set_variable(variables, f"{result_name}.__status", status_value)
        _set_variable(variables, "result.__status", status_value)
        for variable_key, variable_value in (
            ("$result", result_value),
            (f"{var_key(result_name)}.__status", status_value),
            ("$result.__status", status_value),
        ):
            updated_variables.append(variable_key)
            value_previews[variable_key] = _log_preview(variable_value, 300)
    variables["__lastFunctionResult"]["updatedVariables"] = updated_variables
    variables["__lastFunctionResult"]["valuePreviews"] = value_previews
    return bool(result.get("ok"))


def _find_node_in_document(document: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for graph in _as_list(document.get("dialog_flow_graphs")):
        if not isinstance(graph, dict):
            continue
        node = _find_node(graph, node_id)
        if node is not None:
            return node
    return None


def _resolve_structured_source_node_id(
    document: dict[str, Any],
    source_talk_node_id: str | None,
    waiting_node_id: str | None = None,
    message: str | None = None,
) -> str | None:
    if source_talk_node_id:
        return source_talk_node_id
    parsed = _parse_json_or_none(message)
    message_source_node_id = ""
    has_structured_payload = False
    if isinstance(parsed, dict):
        message_source_node_id = str(parsed.get("sourceTalkNodeId") or "").strip()
        has_structured_payload = (
            ("webchatRichFormVersion" in parsed)
            or bool(_as_dict(parsed.get("response")))
            or ("buttonValue" in parsed)
            or isinstance(parsed.get("sourceTalkNodeId"), str)
        )
    if waiting_node_id and (message_source_node_id or has_structured_payload):
        waiting_node = _find_node_in_document(document, waiting_node_id)
        if waiting_node is not None:
            return waiting_node_id
    if message_source_node_id:
        if _find_node_in_document(document, message_source_node_id) is not None:
            return message_source_node_id
    return None


def _store_form_result_variable(document: dict[str, Any], state: dict[str, Any], source_talk_node_id: str | None, message: str, channel_code: str, waiting_talk_node_id: str | None = None) -> tuple[bool, str | None]:
    source_talk_node_id = _resolve_structured_source_node_id(document, source_talk_node_id, waiting_talk_node_id, message)
    source_node = _find_node_in_document(document, source_talk_node_id)
    if source_node is None or source_node.get("kind") != "talk":
        return False, None
    config = _talk_config_for_channel(_as_dict(source_node.get("config")), channel_code)
    if str(config.get("messageType") or "text") not in {"form", "form-a-card"}:
        return False, None
    response_type = str(config.get("responseType") or "none")
    if response_type not in {"form-relay", "single-select"}:
        return False, None
    variable_name = str(config.get("responseVariableName") or "").replace("$", "").strip()
    if not variable_name:
        return False, None
    effective_message = _rich_form_button_value(message) or message
    value = _talk_response_variable_value(config, message, effective_message)
    variables = _as_dict(state.get("variables"))
    state["variables"] = variables
    updated_variables, value_previews = _store_response_variable_value(variables, variable_name, value)
    if response_type != "form-relay":
        return False, None
    graph = _as_dict(_find_graph(document, graph_id=str(state.get("graphId") or ""), dialog_id=str(state.get("dialogId") or "")))
    if not graph or _find_node(graph, str(source_node.get("id") or "")) is None:
        for candidate in _as_list(document.get("dialog_flow_graphs")):
            candidate_graph = _as_dict(candidate)
            if _find_node(candidate_graph, str(source_node.get("id") or "")) is not None:
                graph = candidate_graph
                break
    next_node_id = _next_node_id(graph, str(source_node.get("id") or ""))
    item = _append_runtime_event(
        state,
        level="info",
        event="channel.runtime.form_response_stored",
        message="Form 응답을 변수에 저장했습니다.",
        graph=graph,
        node=source_node,
        data={
            "responseType": response_type,
            "variableName": var_key(variable_name),
            "updatedVariables": updated_variables,
            "valuePreviews": value_previews,
            "nextNodeId": next_node_id,
        },
    )
    _log_runtime_event(item)
    return True, next_node_id


def _handle_runtime_message(
    document: dict[str, Any],
    state: dict[str, Any],
    message: str,
    default_messages: dict[str, str] | None = None,
    channel_code: str = "WEBCHAT",
    button_selection_option: str = "exact",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    variables = _as_dict(state.get("variables"))
    state["variables"] = variables
    waiting_node_id = str(state.get("waitingNodeId") or "")
    graph = _find_graph(document, graph_id=str(state.get("graphId") or ""), dialog_id=str(state.get("dialogId") or ""))
    waiting_node = _find_node(graph, waiting_node_id) if graph else None
    if graph is None or waiting_node is None or waiting_node.get("kind") != "talk":
        return [], state

    config = _talk_config_for_channel(_as_dict(waiting_node.get("config")), channel_code)
    effective_message = _rich_form_button_value(message) or message
    if str(config.get("messageType") or "text") == "dtmf":
        settings = _dtmf_settings(config)
        effective_message = _normalized_dtmf_input(effective_message, settings) or ""
        if not effective_message:
            return [
                {
                    "type": "dtmf",
                    "text": f"{settings['minLength']}~{settings['maxLength']}자리 숫자를 입력하세요.",
                    "options": [],
                    "payload": {
                        "dtmf": settings,
                        "sourceTalkNodeId": waiting_node_id,
                    },
                }
            ], state
    if str(config.get("responseType") or "none") == "single-select":
        options = _single_select_options(config)
        normalized_effective_message = effective_message.strip()
        if button_selection_option == "contains":
            matched = any(option and option in normalized_effective_message for option in options)
        else:
            matched = normalized_effective_message in options
        enforce_single_select_match = not _runtime_transition_locked(document, state) and config.get("intentTransitionLocked") is not True
        if options and not matched and enforce_single_select_match:
            return [
                {
                    "type": "button",
                    "text": _runtime_message(default_messages or {}, "invalid_button", DEFAULT_MESSAGE_FALLBACKS["invalid_button"]),
                    "options": options,
                    "payload": {"sourceTalkNodeId": waiting_node_id},
                }
            ], state

    response_variable_name = str(config.get("responseVariableName") or "").strip()
    updated_variables: list[str] = []
    value_previews: dict[str, str] = {}
    if response_variable_name:
        response_value = _talk_response_variable_value(config, message, effective_message)
        updated_variables, value_previews = _store_response_variable_value(variables, response_variable_name, response_value)
    else:
        _set_variable(variables, "$input", effective_message)
        updated_variables.append("$input")
        value_previews["$input"] = _log_preview(effective_message, 300)
    for extraction in _as_list(config.get("responseEntityExtractions")):
        if isinstance(extraction, dict):
            extraction_variable_name = str(extraction.get("variableName") or "")
            extraction_value = _extract_entity_value(document, extraction, effective_message)
            _set_variable(
                variables,
                extraction_variable_name,
                extraction_value,
            )
            if extraction_variable_name.strip():
                variable_key = var_key(extraction_variable_name)
                updated_variables.append(variable_key)
                value_previews[variable_key] = _log_preview(extraction_value, 300)
    state["waitingNodeId"] = ""
    next_node_id = _next_node_id(graph, waiting_node_id)
    item = _append_runtime_event(
        state,
        level="info",
        event="channel.runtime.talk_response_stored",
        message="사용자 응답을 변수에 저장했습니다.",
        graph=graph,
        node=waiting_node,
        data={
            "responseType": str(config.get("responseType") or "none"),
            "updatedVariables": updated_variables,
            "valuePreviews": value_previews,
            "nextNodeId": next_node_id,
        },
    )
    _log_runtime_event(item)
    return _run_runtime(document, state, next_node_id, default_messages, channel_code)


def _add_message(
    db: Session,
    room: ChannelRoom,
    participant_id: str,
    participant_kind: str,
    participant_name: str,
    text: str,
    message_type: str = "text",
    payload_json: dict[str, Any] | None = None,
) -> ChannelMessage:
    _fence_shared_room_write(db, room)
    created_at = datetime.now(timezone.utc)
    message = ChannelMessage(
        room_id=room.id,
        channel_type=room.channel_type,
        participant_id=participant_id,
        participant_kind=participant_kind,
        participant_name=participant_name,
        message_type=message_type,
        text=text,
        payload_json=payload_json or {},
        created_at=created_at,
    )
    db.add(message)
    _update_room_conversation_history_for_message(room, message)
    context = _shared_turn_capability.get()
    if context is not None and context["room_id"] == room.id:
        context["written_history"] = deepcopy(_as_dict(room.metadata_json).get("conversationHistory"))
    flag_modified(room, "metadata_json")
    db.add(room)
    return message


def _write_channel_audit_log(
    db: Session,
    request: Request,
    action_type: str,
    target_id: Any | None,
    data: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            actor_user_id=None,
            action_type=action_type,
            target_type="channel_runtime",
            target_id=target_id,
            before_json=None,
            after_json=_redact_log_value(data),
            ip_address=request.client.host if request.client else None,
        )
    )


def _create_queue_event(
    db: Session,
    room: ChannelRoom,
    bot: Bot,
    version: BotVersion,
    user_message: ChannelMessage,
    payload: ChannelMessageRequest,
) -> ChannelQueueEvent:
    queue_event = ChannelQueueEvent(
        room_id=room.id,
        request_message_id=user_message.id,
        channel_type=room.channel_type,
        bot_id=bot.id,
        bot_version_id=version.id,
        participant_id=payload.participant_id or room.participant_id,
        sender_system=room.channel_type,
        receiver="Aidot Runtime",
        priority="normal",
        receive_status="received",
        status="queued",
        status_changed_at=datetime.now(timezone.utc),
        parameter_json={
            "message": payload.message,
            "sourceTalkNodeId": payload.source_talk_node_id,
            "clientRoomId": room.client_room_id,
            "targetDialogId": payload.target_dialog_id,
            "dialogParams": payload.dialog_params,
            "systemName": payload.system_name,
            "directDialogRoot": payload.direct_dialog_root,
        },
        result_json={},
    )
    queue_event.room = room
    db.add(queue_event)
    return queue_event


def _room_conversation_history(room: ChannelRoom) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = _as_dict(room.metadata_json)
    history = _as_dict(metadata.get("conversationHistory"))
    metadata["conversationHistory"] = history
    room.metadata_json = metadata
    history["session_id"] = str(room.id)
    history["room_id"] = str(room.id)
    history["client_room_id"] = room.client_room_id
    history["participant_id"] = room.participant_id
    history["participant_name"] = room.participant_name
    history["channel_type"] = room.channel_type
    history["room_status"] = room.status
    history["bot_id"] = str(room.bot_id)
    history["bot_version_id"] = str(room.bot_version_id)
    history.setdefault("started_at", None)
    history.setdefault("first_user_utterance", "")
    if not isinstance(history.get("user_utterances"), list):
        history["user_utterances"] = []
    if not isinstance(history.get("user_raw_utterances"), list):
        history["user_raw_utterances"] = []
    if not isinstance(history.get("transcript"), list):
        history["transcript"] = []
    history.setdefault("user_message_count", 0)
    history.setdefault("message_count", 0)
    history.setdefault("last_message_at", None)
    history.setdefault("last_user_message_at", None)
    history.setdefault("latest_queue_event_id", None)
    history.setdefault("latest_queue_status", None)
    history.setdefault("latest_intent_name", None)
    history.setdefault("start_intent_name", None)
    history.setdefault("start_module_name", None)
    history.setdefault("latest_error_message", None)
    history.setdefault("dialog_ended", False)
    history.setdefault("session_ended", False)
    history.setdefault("completion_reason", "")
    history.setdefault("ended_at", metadata.get("endedAt"))
    history.setdefault("session_end_reason", metadata.get("sessionEndReason"))
    return metadata, history


def _parse_json_like(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed or (not trimmed.startswith("{") and not trimmed.startswith("[")):
        return value
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        return value


def _conversation_start_name(*sources: Any) -> str:
    keys = (
        "sessionStartModuleName",
        "session_start_module_name",
        "sessionStartIntentName",
        "session_start_intent_name",
        "startDialogName",
        "start_dialog_name",
        "startModuleName",
        "start_module_name",
        "startIntentName",
        "start_intent_name",
        "selectedIntentName",
        "selected_intent_name",
        "selectedModuleName",
        "selected_module_name",
        "selectedDialogName",
        "selected_dialog_name",
        "dialogName",
        "dialog_name",
        "moduleName",
        "module_name",
        "intentName",
        "intent_name",
        "cardName",
        "card_name",
        "nodeTitle",
        "node_title",
        "displayName",
        "display_name",
        "name",
    )
    for source in sources:
        record = _as_dict(source)
        if not record:
            continue
        nested_records = [
            record,
            _as_dict(record.get("data")),
            _as_dict(record.get("metadata")),
            _as_dict(record.get("result")),
            _as_dict(record.get("detail")),
            _as_dict(record.get("log")),
            _as_dict(record.get("startDialog") or record.get("start_dialog")),
            _as_dict(record.get("startModule") or record.get("start_module")),
            _as_dict(record.get("startIntent") or record.get("start_intent")),
            _as_dict(record.get("selectedDialog") or record.get("selected_dialog")),
            _as_dict(record.get("selectedModule") or record.get("selected_module")),
            _as_dict(record.get("selectedIntent") or record.get("selected_intent")),
            _as_dict(record.get("dialog") or record.get("intent")),
            _as_dict(record.get("module")),
        ]
        for nested in nested_records:
            for key in keys:
                value = str(nested.get(key) or "").strip()
                if value and value != "-":
                    return value
        runtime_events = record.get("runtimeEvents")
        if isinstance(runtime_events, list):
            for event in runtime_events:
                event_record = _as_dict(event)
                if not event_record:
                    continue
                for key in keys:
                    value = str(event_record.get(key) or "").strip()
                    if value and value != "-":
                        return value
    return ""


def _first_readable_string(value: Any, visited: set[int] | None = None) -> str:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed and "webchatRichFormVersion" not in trimmed:
            return trimmed
        return ""
    if visited is None:
        visited = set()
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in visited:
            return ""
        visited.add(identity)
    if isinstance(value, list):
        for item in value:
            found = _first_readable_string(item, visited)
            if found:
                return found
        return ""
    record = _as_dict(value)
    if not record:
        return ""
    for key in ("label", "text", "title", "name", "value", "buttonValue", "displayValue", "display_text"):
        found = _first_readable_string(record.get(key), visited)
        if found:
            return found
    for entry in record.values():
        found = _first_readable_string(entry, visited)
        if found:
            return found
    return ""


def _summarize_webchat_selection(value: Any) -> str:
    parsed = _parse_json_like(value)
    root = _as_dict(parsed)
    if not str(root.get("webchatRichFormVersion") or "").strip():
        return str(value).strip() if isinstance(value, str) else ""

    response = _as_dict(root.get("response"))
    direct_button_value = _first_readable_string(response.get("buttonValue"))
    if direct_button_value:
        return f"버튼 선택: {direct_button_value}"
    response_entry: tuple[str, Any] | None = None
    for key, entry_value in response.items():
        if isinstance(entry_value, str) and entry_value.strip():
            response_entry = (key, entry_value)
            break
        if isinstance(entry_value, list) and entry_value:
            response_entry = (key, entry_value)
            break
        if _as_dict(entry_value):
            response_entry = (key, entry_value)
            break
    if response_entry is None:
        return "RichForm 응답"

    response_type, response_value = response_entry
    response_record = _as_dict(response_value)
    candidates = (
        response_record.get("buttonValue"),
        response_record.get("displayValue"),
        response_record.get("display_text"),
        response_record.get("text"),
        response_record.get("label"),
        response_record.get("title"),
        response_record.get("value"),
        response_value,
    )
    resolved = next((item for item in (_first_readable_string(candidate) for candidate in candidates) if item), response_type.upper())
    normalized_type = response_type.upper()
    if normalized_type in {"INPUT", "TEXTAREA", "ADDRESS"}:
        return f"입력: {resolved}"
    if normalized_type in {"CHECK", "CHECKBOX", "RADIO", "RADIOBUTTON", "COMBO", "COMBOBOX", "SELECT"}:
        return f"선택: {resolved}"
    if normalized_type in {"TAB", "BUTTON", "TOGGLEBUTTON"}:
        return f"버튼 선택: {resolved}"
    return f"{normalized_type}: {resolved}"


def _summarize_conversation_text(value: Any) -> str:
    if not isinstance(value, str):
        return _first_readable_string(value)
    trimmed = value.strip()
    if not trimmed:
        return ""
    if "webchatRichFormVersion" in trimmed:
        return _summarize_webchat_selection(trimmed)
    return trimmed


def _summarize_richform_payload(payload: Any) -> str:
    payload_record = _as_dict(payload)
    richform = _as_dict(
        payload_record.get("richForm")
        or payload_record.get("richform")
        or payload_record.get("response")
        or payload_record.get("payload")
    )
    title = _first_readable_string(richform.get("title") or richform.get("name") or payload_record.get("title") or payload_record.get("name"))
    text = _first_readable_string(richform.get("text") or richform.get("message") or payload_record.get("text") or payload_record.get("message"))
    options = richform.get("options")
    option_labels = [_first_readable_string(item) for item in options] if isinstance(options, list) else []
    details = [item for item in (title, text, ", ".join(label for label in option_labels[:3] if label)) if item]
    return " / ".join(details) if details else "RichForm 카드"


def _conversation_history_display_text(message: ChannelMessage) -> str:
    participant_kind = str(message.participant_kind or "").lower()
    raw_text = str(message.text or "").strip()
    payload_json = _as_dict(getattr(message, "payload_json", None))
    if participant_kind == "user":
        return _summarize_conversation_text(raw_text or payload_json)
    if not raw_text or raw_text == "RichForm":
        return _summarize_richform_payload(payload_json)
    return _summarize_conversation_text(raw_text)


def _append_room_conversation_transcript(history: dict[str, Any], message: ChannelMessage, created_at: datetime) -> str:
    transcript = history.get("transcript")
    if not isinstance(transcript, list):
        transcript = []
        history["transcript"] = transcript
    display_text = _conversation_history_display_text(message)
    transcript.append(
        {
            "participant_id": getattr(message, "participant_id", None),
            "participant_kind": getattr(message, "participant_kind", None),
            "participant_name": getattr(message, "participant_name", None),
            "message_type": getattr(message, "message_type", None),
            "text": str(getattr(message, "text", "") or ""),
            "payload_json": _as_dict(getattr(message, "payload_json", None)),
            "created_at": created_at.isoformat(),
            "display_text": display_text,
        }
    )
    return display_text


def _update_room_conversation_history_for_message(room: ChannelRoom, message: ChannelMessage) -> None:
    _, history = _room_conversation_history(room)
    created_at = message.created_at or datetime.now(timezone.utc)
    history["room_status"] = room.status
    history["client_room_id"] = room.client_room_id
    history["participant_id"] = room.participant_id
    history["participant_name"] = room.participant_name
    history["last_message_at"] = created_at.isoformat()
    history["message_count"] = int(history.get("message_count") or 0) + 1
    display_text = _append_room_conversation_transcript(history, message, created_at)
    if message.participant_kind != "user":
        return
    text = str(message.text or "").strip()
    history["last_user_message_at"] = created_at.isoformat()
    history["user_message_count"] = int(history.get("user_message_count") or 0) + 1
    if not history.get("started_at"):
        history["started_at"] = created_at.isoformat()
    readable_text = display_text or text
    if readable_text:
        utterances = [str(item) for item in history.get("user_utterances") or [] if str(item).strip()]
        utterances.append(readable_text)
        history["user_utterances"] = utterances
        if not str(history.get("first_user_utterance") or "").strip():
            history["first_user_utterance"] = readable_text
    if text:
        raw_utterances = [str(item) for item in history.get("user_raw_utterances") or [] if str(item).strip()]
        raw_utterances.append(text)
        history["user_raw_utterances"] = raw_utterances


def _set_queue_status(
    queue_event: ChannelQueueEvent,
    status_value: str,
    *,
    receive_status: str | None = None,
    result_json: dict[str, Any] | None = None,
    error_message: str | None = None,
    intent_name: str | None = None,
) -> None:
    redacted_result = _as_dict(_redact_log_value(result_json)) if result_json is not None else None
    redacted_error_message = _mask_sensitive_text(error_message) if error_message is not None else None
    queue_event.status = status_value
    queue_event.receive_status = receive_status or status_value
    queue_event.status_changed_at = datetime.now(timezone.utc)
    if redacted_result is not None:
        queue_event.result_json = redacted_result
    if redacted_error_message is not None:
        queue_event.error_message = redacted_error_message
    if intent_name is not None:
        queue_event.intent_name = intent_name
    room = getattr(queue_event, "room", None)
    if isinstance(room, ChannelRoom):
        _update_room_conversation_history_for_queue_event(
            room,
            queue_event,
            result_json=redacted_result,
            error_message=redacted_error_message,
            intent_name=intent_name,
        )


def _update_room_conversation_history_for_queue_event(
    room: ChannelRoom,
    queue_event: ChannelQueueEvent,
    *,
    result_json: dict[str, Any] | None = None,
    error_message: str | None = None,
    intent_name: str | None = None,
) -> None:
    metadata, history = _room_conversation_history(room)
    history["client_room_id"] = room.client_room_id
    history["room_status"] = room.status
    history["latest_queue_event_id"] = str(queue_event.id)
    history["latest_queue_status"] = queue_event.status
    latest_intent_name = str(intent_name or queue_event.intent_name or history.get("latest_intent_name") or "").strip()
    history["latest_intent_name"] = latest_intent_name or None
    result = _as_dict(result_json if result_json is not None else queue_event.result_json)
    start_name = _conversation_start_name(result, {"intentName": latest_intent_name} if latest_intent_name else {})
    if not str(history.get("start_intent_name") or "").strip() and latest_intent_name:
        history["start_intent_name"] = latest_intent_name
    if not str(history.get("start_module_name") or "").strip() and start_name and start_name != latest_intent_name:
        history["start_module_name"] = start_name
    history["dialog_ended"] = result.get("dialogEnded") is True
    history["session_ended"] = result.get("sessionEnded") is True
    history["completion_reason"] = str(result.get("completionReason") or "")
    if error_message is not None:
        history["latest_error_message"] = error_message
    if history["session_ended"] or room.status == "closed":
        history["ended_at"] = metadata.get("endedAt") or datetime.now(timezone.utc).isoformat()
        history["session_end_reason"] = metadata.get("sessionEndReason") or history.get("session_end_reason")
    if hasattr(room, "_sa_instance_state"):
        flag_modified(room, "metadata_json")


def _apply_active_version_to_room(
    room: ChannelRoom,
    bot: Bot,
    version: BotVersion,
    channel: str,
) -> str:
    previous_version_id = str(room.bot_version_id)
    if hasattr(room, "_sa_instance_state"):
        from sqlalchemy.orm import object_session
        from app.models import ChannelLlmBinding
        room_db = object_session(room)
        if room_db is not None and room_db.scalar(select(ChannelLlmBinding.id).where(ChannelLlmBinding.room_id == room.id, ChannelLlmBinding.status != "closed").limit(1)) is not None:
            return previous_version_id
    runtime_state = _initial_runtime_state_for_version(bot, version, channel)
    room.bot_version_id = version.id
    _set_runtime_system_variables(
        runtime_state,
        bot=bot,
        channel_code=_channel_template_code(channel),
        room=room,
        participant_id=room.participant_id,
        participant_name=room.participant_name,
    )
    item = _append_runtime_event(
        runtime_state,
        level="info",
        event="channel.runtime.active_version_applied",
        message="운영 버전을 현재 채널 세션에 적용했습니다.",
        data={
            "previousVersionId": previous_version_id,
            "activeVersionId": str(version.id),
            "channel": channel,
        },
    )
    _log_runtime_event(item)
    credential = _as_dict(room.metadata_json).get("__webchatAccess")
    if isinstance(credential, dict):
        runtime_state["__webchatAccess"] = deepcopy(credential)
    room.metadata_json = runtime_state
    if hasattr(room, "_sa_instance_state"):
        flag_modified(room, "metadata_json")
    return previous_version_id

def _archive_room_for_new_session(db: Session, room: ChannelRoom, reason: str) -> None:
    metadata = _as_dict(room.metadata_json)
    if room.client_room_id:
        metadata["originalClientRoomId"] = room.client_room_id
        room.client_room_id = f"{room.client_room_id}::closed::{room.id}"
    metadata["sessionEndReason"] = reason
    metadata["endedAt"] = datetime.now(timezone.utc).isoformat()
    room.metadata_json = metadata
    room.status = "closed"
    room.deleted_at = datetime.now(timezone.utc)
    _, history = _room_conversation_history(room)
    history["client_room_id"] = room.client_room_id
    history["room_status"] = room.status
    history["ended_at"] = metadata["endedAt"]
    history["session_end_reason"] = reason
    if not history.get("session_ended"):
        history["session_ended"] = True
    flag_modified(room, "metadata_json")
    db.add(room)

def _add_configured_initial_messages(
    db: Session,
    room: ChannelRoom,
    bot: Bot,
    version: BotVersion,
    channel: str,
) -> list[ChannelMessage]:
    languages = _message_language_candidates(None, None, bot, room)
    default_messages = _load_default_messages(db, bot.organization_id, languages)
    configured_messages = _initial_messages_for_version(bot, version, default_messages, channel)
    bot_messages: list[ChannelMessage] = []
    for output in configured_messages:
        bot_messages.append(
            _add_message(
                db,
                room,
                participant_id=str(bot.id),
                participant_kind="bot",
                participant_name=bot.name,
                text=str(output.get("text") or ""),
                message_type=str(output.get("type") or "text"),
                payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
            )
        )
    return bot_messages


def _ensure_room_started(
    db: Session,
    room: ChannelRoom,
    bot: Bot,
    version: BotVersion,
    channel: str,
    messages: list[ChannelMessage] | None = None,
) -> list[ChannelMessage]:
    if messages is None:
        messages = db.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.room_id == room.id, ChannelMessage.deleted_at.is_(None))
            .order_by(ChannelMessage.created_at.asc())
        ).all()
    if messages:
        return messages

    if is_hub_bot(bot):
        hub_output = hub_selection_output(db, bot)
        runtime_state = _as_dict(room.metadata_json) or _initial_runtime_state_for_version(bot, version, channel)
        runtime_state["hubSelectionPending"] = True
        runtime_state["hubBotId"] = str(bot.id)
        _set_runtime_system_variables(runtime_state, bot=bot, channel_code=_channel_template_code(channel), room=room)
        room.metadata_json = runtime_state
        flag_modified(room, "metadata_json")
        messages.append(
            _add_message(
                db, room, str(bot.id), "bot", bot.name,
                str(hub_output.get("text") or ""),
                str(hub_output.get("type") or "text"),
                {"options": hub_output.get("options") or [], **_as_dict(hub_output.get("payload"))},
            )
        )
        return messages

    runtime_state = _as_dict(room.metadata_json)
    if not runtime_state.get("currentNodeId"):
        runtime_state = _initial_runtime_state_for_version(bot, version, channel)
    _set_runtime_system_variables(runtime_state, bot=bot, channel_code=_channel_template_code(channel), room=room)
    document = normalize_version_document(version.version_json)
    languages = _message_language_candidates(None, None, bot, room)
    default_messages = _load_default_messages(db, bot.organization_id, languages)
    channel_code = _channel_template_code(channel)
    runtime_outputs, runtime_state = _run_runtime(
        document,
        runtime_state,
        str(runtime_state.get("currentNodeId") or ""),
        default_messages,
        channel_code,
    )
    room.metadata_json = runtime_state
    flag_modified(room, "metadata_json")

    for output in runtime_outputs:
        messages.append(
            _add_message(
                db,
                room,
                participant_id=str(bot.id),
                participant_kind="bot",
                participant_name=bot.name,
                text=str(output.get("text") or ""),
                message_type=str(output.get("type") or "text"),
                payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
            )
        )
    return messages


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _compact_text(value: object) -> str:
    return re.sub(r"[^\w]+", "", _normalize_text(value), flags=re.UNICODE)


def _semantic_match_key(value: object) -> str:
    return re.sub(r"[\W_]+", "", _normalize_text(value), flags=re.UNICODE)


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
    return {token for token in re.split(r"[^\w]+", _normalize_text(value), flags=re.UNICODE) if token}


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
    search_started_at = time.perf_counter()
    matches = client.search(
        bot_id=str(bot.id),
        version_id=str(version.id),
        query=message,
        top_k=top_k,
        dictionary_terms=dictionary_terms,
    )
    elapsed_ms = _elapsed_ms(search_started_at)
    log_payload = {
        "event": "channel.semantic_nlu.search_completed",
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "query_length": len(message),
        "top_k": top_k,
        "result_count": len(matches),
        "elapsed_ms": elapsed_ms,
    }
    if elapsed_ms >= settings.api_slow_request_threshold_ms:
        logger.warning("Semantic vector intent search slow.", extra=log_payload)
    else:
        logger.info("Semantic vector intent search completed.", extra=log_payload)
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
    cutoff_score = _nlu_cutoff_score(bot, version)

    def with_cutoff(result: tuple[dict[str, Any] | None, float]) -> tuple[dict[str, Any] | None, float]:
        dialog, score = result
        if dialog is None or score < cutoff_score:
            return None, score
        return dialog, score

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
            return with_cutoff(_semantic_select_dialog(document, bot, version, message))
        except VectorSearchError as error:
            if isinstance(data_json.get("secondary_llm"), dict) and _as_dict(bot.data_json).get("bot_kind") != "hub":
                raise
            logger.warning(
                "Semantic vector intent search failed.",
                extra={
                    "event": "channel.semantic_nlu.search_failed",
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
            return with_cutoff(_llm_select_dialog(document, bot, version, message))
        except LlmClientError as error:
            logger.warning(
                "LLM intent classification failed.",
                extra={
                    "event": "channel.llm_nlu.classification_failed",
                    "extra_data": {"bot_id": str(bot.id), "version_id": str(version.id), "error": str(error)},
                },
            )
            return None, 0.0
    nlu_model = str(data_json.get("nlu_model") or data_json.get("nluModel") or "deep_learning_lite")
    if nlu_type == "ml" and nlu_model == "deep_learning_lite":
        candidate = classify_deep_learning_lite_model(
            version,
            message,
            version_settings=_version_settings(bot, version),
            language=str((bot.data_json or {}).get("language") or "ko"),
        )
        if candidate is None:
            return None, 0.0
        dialog = _dialog_by_id_or_name(
            document,
            str(candidate.get("dialog_id") or ""),
            str(candidate.get("dialog_name") or ""),
        )
        if dialog is None:
            return None, 0.0
        return with_cutoff((dialog, float(candidate.get("score") or 0.0)))
    return with_cutoff(_select_dialog(document, message, prefer_exact_utterance=prefer_exact_utterance))


def _select_natural_hub_candidates(
    db: Session,
    hub_bot: Bot,
    message: str,
) -> tuple[Any | None, list[dict[str, Any]]]:
    hub = hub_configuration(db, hub_bot)
    if hub is None:
        return None, []

    hub_version = db.get(BotVersion, hub_bot.active_version_id) if hub_bot.active_version_id else None
    hub_system_config = _as_dict(getattr(hub_version, "system_config_json", None)) if hub_version is not None else {}
    hub_routing = _as_dict(hub_system_config.get("hub_routing"))
    hub_routes = {
        str(item.get("dialogId") or ""): item
        for item in _as_list(hub_routing.get("members"))
        if isinstance(item, dict) and str(item.get("dialogId") or "").strip()
    }
    hub_ai_config = _version_ai_config(hub_bot, hub_version) if hub_version is not None else {}
    hub_uses_ml_snapshot = str(hub_ai_config.get("nlu_type") or hub_ai_config.get("nluType") or "ml").lower() == "ml" and str(hub_ai_config.get("nlu_model") or hub_ai_config.get("nluModel") or "deep_learning_lite") == "deep_learning_lite"
    if hub_version is not None and hub_routes and hub_uses_ml_snapshot:
        active_members = active_hub_members(db, hub_bot.id)
        members_by_id = {str(member_bot.id): (member, member_bot, member_version) for member, member_bot, member_version in active_members}
        if {str(item.get("botId") or "") for item in hub_routes.values()} != set(members_by_id):
            return hub, []

        hub_document = normalize_version_document(hub_version.version_json)
        hub_scores = score_deep_learning_lite_model(
            hub_version,
            message,
            version_settings=_version_settings(hub_bot, hub_version),
            language=str((hub_bot.data_json or {}).get("language") or "ko"),
        )
        candidates: list[dict[str, Any]] = []
        for score in hub_scores:
            if float(score.get("score") or 0.0) < float(hub.intent_cutoff_score):
                continue
            route = hub_routes.get(str(score.get("dialog_id") or ""))
            if route is None:
                continue
            selected_member = members_by_id.get(str(route.get("botId") or ""))
            if selected_member is None:
                continue
            member, member_bot, member_version = selected_member
            if str(member_version.id) != str(route.get("botVersionId") or ""):
                continue
            member_document = normalize_version_document(member_version.version_json)
            dialog, _ = _select_dialog_for_bot(
                member_document,
                member_bot,
                member_version,
                message,
                prefer_exact_utterance=_exacting_matching_enabled(member_bot, member_version),
            )
            if dialog is not None:
                candidates.append({"member": member, "bot": member_bot, "version": member_version, "dialog": dialog, "score": float(score.get("score") or 0.0)})
        candidates.sort(key=lambda item: item["score"], reverse=True)
        return hub, candidates

    candidates: list[dict[str, Any]] = []
    for member, member_bot, member_version in active_hub_members(db, hub_bot.id):
        smalltalk = _smalltalk_match(member_bot, member_version, message)
        if smalltalk is not None:
            candidates.append(
                {
                    "member": member,
                    "bot": member_bot,
                    "version": member_version,
                    "dialog": None,
                    "score": 1.0,
                    "smalltalk": smalltalk,
                }
            )
            continue
        member_document = normalize_version_document(member_version.version_json)
        dialog, score = _select_dialog_for_bot(
            member_document,
            member_bot,
            member_version,
            message,
            prefer_exact_utterance=_exacting_matching_enabled(member_bot, member_version),
        )
        if dialog is None or score < float(hub.intent_cutoff_score):
            continue
        candidates.append(
            {
                "member": member,
                "bot": member_bot,
                "version": member_version,
                "dialog": dialog,
                "score": float(score),
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return hub, candidates


def _reply_for_dialog(
    db: Session,
    bot: Bot,
    dialog: dict[str, Any] | None,
    languages: tuple[str, ...] = ("ko",),
) -> str:
    if dialog is None:
        return get_default_message_text(db, bot.organization_id, "intent_fallback", languages=languages)
    for key in ("fallbackResponse", "response", "answer", "message"):
        value = dialog.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    name = str(dialog.get("displayName") or dialog.get("name") or "의도").strip()
    template = get_default_message_text(
        db,
        bot.organization_id,
        "intent_receipt",
        languages=languages,
        fallback="{{intentName}} 의도로 접수되었습니다.",
    )
    return template.replace("{{intentName}}", name)


def _graph_for_dialog(document: dict[str, Any], dialog: dict[str, Any]) -> dict[str, Any] | None:
    return _find_graph(
        document,
        dialog_id=str(dialog.get("id") or ""),
        name=str(dialog.get("name") or dialog.get("displayName") or ""),
    )


def _direct_dialog_definition(document: dict[str, Any], dialog_id: str) -> dict[str, Any] | None:
    dialog = _dialog_by_id_or_name(document, dialog_id)
    if dialog is not None:
        return dialog
    graph = _find_graph(document, dialog_id=dialog_id)
    if graph is None:
        return None
    return {
        "id": str(graph.get("dialogId") or dialog_id),
        "name": str(graph.get("name") or dialog_id),
        "displayName": str(graph.get("displayName") or graph.get("name") or dialog_id),
    }


def _run_direct_dialog(
    document: dict[str, Any],
    state: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    dialog: dict[str, Any],
    dialog_params: dict[str, Any],
    default_messages: dict[str, str],
    channel_code: str,
    return_to_previous: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    graph = _graph_for_dialog(document, dialog)
    start_node_id = _first_runtime_node_id(graph) if graph else None
    if graph is None or not start_node_id:
        raise ValueError(f"대화 시작 그래프를 찾을 수 없습니다: {dialog.get('id')}")

    if return_to_previous:
        _queue_runtime_return(document, state, dialog)
    state["__runtimeTransitionLocked"] = state.get("__runtimeTransitionLocked") is True or bool(
        dialog.get("transitionLocked") is True
    )
    state["__runtimeReturnBlocked"] = bool(dialog.get("returnBlocked") is True)
    state["graphId"] = str(graph.get("id") or "")
    state["dialogId"] = str(graph.get("dialogId") or dialog.get("id") or "")
    state["waitingNodeId"] = ""
    variables = _as_dict(state.get("variables"))
    state["variables"] = variables
    _set_variable(variables, "$input", "")
    for key, value in dialog_params.items():
        _set_variable(variables, str(key), value)
    _prepare_answer_rag_variables(state, bot, version, "", dialog)
    return _run_runtime(document, state, start_node_id, default_messages, channel_code)


def _module_dialog_by_name(document: dict[str, Any], module_name: str | None) -> dict[str, Any] | None:
    normalized_name = str(module_name or "").strip()
    if not normalized_name:
        return None
    for dialog in _safe_dialogs(document):
        if str(dialog.get("dialogType") or "") not in {"0", "0.0"}:
            continue
        names = {
            str(dialog.get("id") or "").strip(),
            str(dialog.get("name") or "").strip(),
            str(dialog.get("displayName") or "").strip(),
            str(dialog.get("dialogKey") or "").strip(),
        }
        if normalized_name in names:
            return dialog
    return None


def _graph_for_module_name(document: dict[str, Any], module_name: str | None) -> dict[str, Any] | None:
    module_dialog = _module_dialog_by_name(document, module_name)
    if module_dialog is None:
        return None
    return _graph_for_dialog(document, module_dialog)


def _should_run_intent_fallback(
    runtime_outputs: list[dict[str, Any]],
    form_response_handled: bool,
    was_waiting_for_talk_node: bool,
    is_structured_input: bool = False,
) -> bool:
    if runtime_outputs or form_response_handled or is_structured_input:
        return False
    return not was_waiting_for_talk_node


def _talk_response_type_supports_interrupt(response_type: str) -> bool:
    return response_type in {"single-select", "relay", "extract-entity", "form-relay"}


def _runtime_return_stack(state: dict[str, Any]) -> list[dict[str, Any]]:
    stack = state.get("__returnRuntimeStack")
    if not isinstance(stack, list):
        stack = []
    state["__returnRuntimeStack"] = stack
    return stack


def _capture_runtime_return_frame(state: dict[str, Any]) -> dict[str, Any] | None:
    graph_id = str(state.get("graphId") or "").strip()
    dialog_id = str(state.get("dialogId") or "").strip()
    if not graph_id or not dialog_id:
        return None
    return {
        "graphId": graph_id,
        "dialogId": dialog_id,
        "currentNodeId": str(state.get("currentNodeId") or "").strip(),
        "waitingNodeId": str(state.get("waitingNodeId") or "").strip(),
        "intentFallbackCount": int(state.get("intentFallbackCount") or 0),
        "runtimeTransitionLocked": state.get("__runtimeTransitionLocked") is True,
        "runtimeReturnBlocked": state.get("__runtimeReturnBlocked") is True,
    }


def _runtime_return_blocked(document: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("__runtimeReturnBlocked") is True:
        return True
    dialog = _runtime_dialog(document, state)
    if dialog is not None and dialog.get("returnBlocked") is True:
        state["__runtimeReturnBlocked"] = True
        return True
    return False


def _queue_runtime_return(document: dict[str, Any], state: dict[str, Any], target_dialog: dict[str, Any] | None) -> None:
    if not isinstance(target_dialog, dict):
        return
    if _runtime_return_blocked(document, state):
        return
    frame = _capture_runtime_return_frame(state)
    if frame is None:
        return
    if frame["dialogId"] == str(target_dialog.get("id") or "").strip():
        return
    stack = _runtime_return_stack(state)
    if stack and str(_as_dict(stack[-1]).get("dialogId") or "") == frame["dialogId"]:
        return
    stack.append(frame)


def _restore_runtime_return(state: dict[str, Any]) -> bool:
    stack = _runtime_return_stack(state)
    if not stack:
        return False
    frame = _as_dict(stack.pop())
    graph_id = str(frame.get("graphId") or "").strip()
    dialog_id = str(frame.get("dialogId") or "").strip()
    if not graph_id or not dialog_id:
        return False
    state["graphId"] = graph_id
    state["dialogId"] = dialog_id
    state["currentNodeId"] = str(frame.get("currentNodeId") or "").strip()
    state["waitingNodeId"] = str(frame.get("waitingNodeId") or "").strip()
    state["dialogEnded"] = False
    state["sessionEnded"] = False
    state["intentFallbackCount"] = int(frame.get("intentFallbackCount") or 0)
    state["__runtimeTransitionLocked"] = frame.get("runtimeTransitionLocked") is True
    state["__runtimeReturnBlocked"] = frame.get("runtimeReturnBlocked") is True
    return True


def _runtime_dialog(document: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    dialog_id = str(state.get("dialogId") or "").strip()
    if not dialog_id:
        return None
    return next(
        (
            dialog
            for dialog in _safe_dialogs(document)
            if str(dialog.get("id") or "").strip() == dialog_id
        ),
        None,
    )


def _runtime_transition_locked(document: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("__runtimeTransitionLocked") is True:
        return True
    dialog = _runtime_dialog(document, state)
    if dialog is not None and dialog.get("transitionLocked") is True:
        state["__runtimeTransitionLocked"] = True
        return True
    return False


def _waiting_talk_node(document: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    waiting_node_id = str(state.get("waitingNodeId") or "").strip()
    if not waiting_node_id:
        return None
    graph = _find_graph(
        document,
        graph_id=str(state.get("graphId") or ""),
        dialog_id=str(state.get("dialogId") or ""),
    )
    node = _find_node(graph, waiting_node_id) if graph else None
    if node is None or str(node.get("kind") or "") != "talk":
        return None
    return node


def _reset_stalled_waiting_talk_state(document: dict[str, Any], state: dict[str, Any], channel_code: str) -> bool:
    if str(state.get("waitingNodeId") or "").strip():
        return False
    graph = _find_graph(
        document,
        graph_id=str(state.get("graphId") or ""),
        dialog_id=str(state.get("dialogId") or ""),
    )
    node = _find_node(graph, str(state.get("currentNodeId") or "")) if graph else None
    if node is None or str(node.get("kind") or "") != "talk":
        return False
    config = _talk_config_for_channel(_as_dict(node.get("config")), channel_code)
    if not _talk_should_wait(config):
        return False

    state["graphId"] = ""
    state["dialogId"] = ""
    state["currentNodeId"] = ""
    state["waitingNodeId"] = ""
    state["dialogEnded"] = False
    state.pop("__runtimeTransitionLocked", None)
    state.pop("__runtimeReturnBlocked", None)
    state["__returnRuntimeStack"] = []
    return True

def _waiting_talk_allows_intent_transition(
    document: dict[str, Any],
    state: dict[str, Any],
    channel_code: str,
) -> bool:
    waiting_node = _waiting_talk_node(document, state)
    if waiting_node is None:
        return False
    if _runtime_transition_locked(document, state):
        return False
    config = _talk_config_for_channel(_as_dict(waiting_node.get("config")), channel_code)
    response_type = str(config.get("responseType") or "none")
    if not _talk_response_type_supports_interrupt(response_type):
        return False
    return config.get("intentTransitionLocked") is not True


def _runtime_allows_general_intent_transition(
    document: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    if state.get("dialogEnded") is True:
        return True
    return not _runtime_transition_locked(document, state)


def _interrupt_waiting_talk_with_intent(
    document: dict[str, Any],
    state: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    default_messages: dict[str, str],
    channel_code: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None, bool]:
    if not _waiting_talk_allows_intent_transition(document, state, channel_code):
        return [], state, None, False

    score = 1.0
    runtime_outputs, runtime_state, selected_dialog, handled_pre_nlu, nlu_message = _handle_pre_nlu_settings(
        document,
        state,
        bot,
        version,
        message,
        default_messages,
        channel_code,
    )
    if not handled_pre_nlu:
        selected_dialog, score = _select_dialog_for_bot(
            document,
            bot,
            version,
            nlu_message,
            prefer_exact_utterance=_exacting_matching_enabled(bot, version),
        )

    if selected_dialog is None and not runtime_outputs:
        return [], state, None, False

    if selected_dialog is not None and not runtime_outputs:
        item = _append_runtime_event(
            runtime_state,
            level="info",
            event="channel.runtime.intent_transition_matched",
            message="대기 중 Talk 응답보다 의도 전환을 우선 적용했습니다.",
            data={
                "intentId": str(selected_dialog.get("id") or ""),
                "intentName": str(selected_dialog.get("name") or selected_dialog.get("displayName") or ""),
                "intentScore": round(score * 100, 2),
            },
        )
        _log_runtime_event(item)
        runtime_state["intentFallbackCount"] = 0
        intent_graph = _graph_for_dialog(document, selected_dialog)
        start_node_id = _first_runtime_node_id(intent_graph) if intent_graph else None
        if intent_graph is not None and start_node_id:
            _queue_runtime_return(document, runtime_state, selected_dialog)
            runtime_state["__runtimeTransitionLocked"] = runtime_state.get("__runtimeTransitionLocked") is True or bool(
                selected_dialog.get("transitionLocked") is True
            )
            runtime_state["__runtimeReturnBlocked"] = bool(selected_dialog.get("returnBlocked") is True)
            runtime_state["graphId"] = str(intent_graph.get("id") or "")
            runtime_state["dialogId"] = str(intent_graph.get("dialogId") or selected_dialog.get("id") or "")
            runtime_state["waitingNodeId"] = ""
            variables = _as_dict(runtime_state.get("variables"))
            runtime_state["variables"] = variables
            _set_variable(variables, "$input", nlu_message)
            _prepare_answer_rag_variables(runtime_state, bot, version, nlu_message, selected_dialog)
            runtime_outputs, runtime_state = _run_runtime(
                document,
                runtime_state,
                start_node_id,
                default_messages,
                channel_code,
            )

    return runtime_outputs, runtime_state, selected_dialog, True


def _is_structured_talk_input(
    document: dict[str, Any],
    source_talk_node_id: str | None,
    message: str,
    channel_code: str,
    waiting_talk_node_id: str | None = None,
) -> bool:
    parsed = _parse_json_or_none(message)
    message_source_node_id = ""
    if isinstance(parsed, dict):
        message_source_node_id = str(parsed.get("sourceTalkNodeId") or "").strip()

    has_explicit_structured_source = bool(source_talk_node_id or message_source_node_id)
    has_structured_payload = isinstance(parsed, dict) and (
        ("webchatRichFormVersion" in parsed)
        or bool(_as_dict(parsed.get("response")))
        or ("buttonValue" in parsed)
        or isinstance(parsed.get("sourceTalkNodeId"), str)
    )
    if not has_explicit_structured_source and not has_structured_payload and _rich_form_button_value(message) is None:
        return False

    source_talk_node_id = _resolve_structured_source_node_id(
        document,
        source_talk_node_id,
        waiting_talk_node_id,
        message,
    )
    source_node = _find_node_in_document(document, source_talk_node_id)
    if source_node is None or str(source_node.get("kind") or "") != "talk":
        return has_structured_payload or _rich_form_button_value(message) is not None

    config = _talk_config_for_channel(_as_dict(source_node.get("config")), channel_code)
    response_type = str(config.get("responseType") or "none")
    if response_type in {"form-relay", "single-select", "relay", "extract-entity"}:
        return True
    return has_structured_payload or _rich_form_button_value(message) is not None


def _run_named_module(
    document: dict[str, Any],
    state: dict[str, Any],
    module_name: str | None,
    default_messages: dict[str, str],
    channel_code: str,
    *, module_id_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    if module_id_only:
        dialog = next((d for d in _as_list(document.get("dialogs")) if isinstance(d, dict) and str(d.get("id") or "") == module_name and d.get("dialogType") in (0, "0")), None)
        module_graph = _graph_for_dialog(document, dialog) if dialog else None
    else:
        module_graph = _graph_for_module_name(document, module_name)
    if module_graph is None:
        return [], state, False
    variables = _as_dict(state.get("variables"))
    module_state = {
        **state,
        "graphId": str(module_graph.get("id") or ""),
        "dialogId": str(module_graph.get("dialogId") or ""),
        "currentNodeId": str(_first_runtime_node_id(module_graph) or ""),
        "waitingNodeId": "",
        "variables": variables,
    }
    outputs, next_state = _run_runtime(
        document,
        module_state,
        str(module_state.get("currentNodeId") or ""),
        default_messages,
        channel_code,
    )
    return outputs, next_state, True


def _handle_intent_not_found(
    document: dict[str, Any],
    state: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    default_messages: dict[str, str],
    channel_code: str,
    *, excluded_module_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    settings = _intent_detection_settings(bot, version)
    retry_count = max(1, _int_setting(settings.get("retryCount"), 2))
    current_count = _int_setting(state.get("intentFallbackCount"), 0) + 1
    state["intentFallbackCount"] = current_count
    if current_count < retry_count:
        return [], state, False
    excluded = next((d for d in _as_list(document.get("dialogs")) if isinstance(d, dict) and str(d.get("id") or "") == excluded_module_id), {})
    if excluded_module_id and str(settings.get("overflowModule") or "") in {str(excluded.get(k) or "") for k in ("id", "name", "displayName", "dialogKey")} - {""}:
        return [], state, False
    outputs, next_state, ran = _run_named_module(
        document,
        state,
        str(settings.get("overflowModule") or ""),
        default_messages,
        channel_code,
    )
    if ran:
        next_state["intentFallbackCount"] = 0
    return outputs, next_state, ran



def _apply_secondary_runtime_result(document, result, bot, version, default_messages, channel_code, module_id):
    state = result.state
    item = _append_runtime_event(state, level="info", event="channel.runtime.secondary_result", message="2차 모듈 실행 결과를 판정했습니다.", data={"status": result.status, "moduleId": module_id})
    _log_runtime_event(item)
    if result.status == "unrecognized":
        outputs, state, _ = _handle_intent_not_found(document, state, bot, version, default_messages, channel_code, excluded_module_id=module_id)
        return SecondaryOutcome("unrecognized", outputs, state)
    if result.status == "error":
        state.update(waitingNodeId="", currentNodeId="", dialogEnded=True)
        return SecondaryOutcome("error", [_runtime_system_error_output(default_messages)], state)
    if result.status == "answered":
        state["intentFallbackCount"] = 0
    return result


def _handle_local_intent_miss(document, state, bot, version, default_messages, channel_code):
    config = _version_ai_config(bot, version).get("secondary_llm")
    if not isinstance(config, dict) or _as_dict(bot.data_json).get("bot_kind") == "hub":
        outputs, state, _ = _handle_intent_not_found(document, state, bot, version, default_messages, channel_code)
        return SecondaryOutcome("unrecognized", outputs, state)
    def run_module(*args):
        return _run_named_module(*args, module_id_only=True)
    result = run_secondary_module(document, state, config, run_module=run_module, default_messages=default_messages, channel_code=channel_code)
    return _apply_secondary_runtime_result(document, result, bot, version, default_messages, channel_code, config.get("module_id"))


def _finish_secondary_runtime(document, state, bot, version, outputs, default_messages, channel_code):
    config = _as_dict(_as_dict(state.get("__secondaryLlm")).get("config"))
    result = resume_secondary_outcome(state, outputs)
    return _apply_secondary_runtime_result(document, result, bot, version, default_messages, channel_code, config.get("module_id"))


def _handle_pre_nlu_settings(
    document: dict[str, Any],
    state: dict[str, Any],
    bot: Bot,
    version: BotVersion,
    message: str,
    default_messages: dict[str, str],
    channel_code: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None, bool, str]:
    smalltalk = _smalltalk_match(bot, version, message)
    if smalltalk is not None:
        item = _append_runtime_event(
            state,
            level="info",
            event="channel.runtime.smalltalk_matched",
            message="스몰토크에 매칭되어 응답을 반환합니다.",
            data={"smalltalkId": str(smalltalk.get("id") or ""), "smalltalkTitle": str(smalltalk.get("title") or "")},
        )
        _log_runtime_event(item)
        return [{"type": "text", "text": str(smalltalk.get("response") or ""), "options": []}], state, None, True, message

    effective_message, applied_blocklists = _apply_blocklist_patterns(bot, version, message)
    if applied_blocklists:
        item = _append_runtime_event(
            state,
            level="info",
            event="channel.runtime.blocklist_applied",
            message="제외/무시 목록을 적용한 발화로 의도 인식을 진행합니다.",
            data={
                "blocklistIds": [str(blocklist.get("id") or "") for blocklist in applied_blocklists],
                "blocklistNames": [str(blocklist.get("name") or "") for blocklist in applied_blocklists],
                "originalMessagePreview": _log_preview(message, 500),
                "effectiveMessagePreview": _log_preview(effective_message, 500),
            },
        )
        _log_runtime_event(item)

    if _exacting_matching_enabled(bot, version):
        exact_dialog = _exact_utterance_dialog_for_bot(document, bot, version, effective_message)
        if exact_dialog is not None:
            item = _append_runtime_event(
                state,
                level="info",
                event="channel.runtime.exacting_matching_matched",
                message="학습문장 완전 일치로 대화 흐름을 선택했습니다.",
                data={
                    "intentId": str(exact_dialog.get("id") or ""),
                    "intentName": str(exact_dialog.get("name") or exact_dialog.get("displayName") or ""),
                },
            )
            _log_runtime_event(item)
            return [], state, exact_dialog, True, effective_message

    rule = _rule_match(bot, version, message)
    if rule is not None:
        target = str(rule.get("target") or "").strip()
        target_dialog = _module_dialog_by_name(document, target)
        if target_dialog is None:
            target_dialog = next(
                (
                    dialog
                    for dialog in _safe_dialogs(document)
                    if str(dialog.get("dialogType") or "1") in {"1", "1.0"}
                    and target
                    in {
                        str(dialog.get("id") or "").strip(),
                        str(dialog.get("name") or "").strip(),
                        str(dialog.get("displayName") or "").strip(),
                        str(dialog.get("dialogKey") or "").strip(),
                    }
                ),
                None,
            )
        if target_dialog is not None:
            item = _append_runtime_event(
                state,
                level="info",
                event="channel.runtime.rule_matched",
                message="룰에 매칭되어 연결 대화로 이동합니다.",
                data={
                    "ruleId": str(rule.get("id") or ""),
                    "ruleName": str(rule.get("name") or ""),
                    "target": target,
                },
            )
            _log_runtime_event(item)
            return [], state, target_dialog, True, effective_message
    return [], state, None, False, effective_message

def _process_channel_queue_event(
    db: Session,
    request: Request,
    channel: str,
    queue_event: ChannelQueueEvent,
) -> dict[str, Any]:
    if (getattr(queue_event, "operation_kind", None) or "channel") != "channel":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널 작업을 찾을 수 없습니다.")
    _assert_legacy_room_access(db, queue_event.room_id)
    channel_code = _channel_template_code(channel)
    room = db.get(ChannelRoom, queue_event.room_id) if queue_event.room_id else None
    bot = db.get(Bot, queue_event.bot_id)
    version = db.get(BotVersion, queue_event.bot_version_id)
    if room is None or bot is None or version is None:
        _set_queue_status(queue_event, "failed", receive_status="failed", error_message="Queue 처리에 필요한 채팅방 또는 봇 정보를 찾지 못했습니다.")
        return {"queueEvent": {"id": str(queue_event.id), "status": queue_event.status, "receiveStatus": queue_event.receive_status}, "botMessages": []}
    queue_event.room = room

    parameter_json = _as_dict(queue_event.parameter_json)
    payload = ChannelMessageRequest(
        message=str(parameter_json.get("message") or ""),
        participant_id=queue_event.participant_id or room.participant_id,
        source_talk_node_id=str(parameter_json.get("sourceTalkNodeId") or "") or None,
        target_dialog_id=str(parameter_json.get("targetDialogId") or "") or None,
        dialog_params=_as_dict(parameter_json.get("dialogParams")),
        system_name=str(parameter_json.get("systemName") or "") or None,
        direct_dialog_root=bool(parameter_json.get("directDialogRoot")),
    )
    _set_queue_status(queue_event, "processing", receive_status="processing")

    hub_call_rule_match = _find_hub_call_rule(db, bot, payload.message)
    hub_call_rule_triggered = hub_call_rule_match is not None
    if hub_call_rule_match is not None:
        source_bot = bot
        bot, version, hub_call_rule = hub_call_rule_match
        room.bot_id = bot.id
        room.bot_version_id = version.id
        queue_event.bot_id = bot.id
        queue_event.bot_version_id = version.id
        queue_event.parameter_json = {
            **parameter_json,
            "hubCallRule": {
                "sourceBotId": str(source_bot.id),
                "hubBotId": str(bot.id),
                "hubBotName": bot.name,
                "ruleId": str(hub_call_rule.get("id") or ""),
                "ruleName": str(hub_call_rule.get("name") or ""),
                "expression": str(hub_call_rule.get("expression") or ""),
            },
        }
        parameter_json = _as_dict(queue_event.parameter_json)
        flag_modified(queue_event, "parameter_json")

    document = normalize_version_document(version.version_json)
    room_languages = _message_language_candidates(None, None, bot, room)
    default_messages = _load_default_messages(db, bot.organization_id, room_languages)
    runtime_state_before = _as_dict(room.metadata_json) or _initial_runtime_state_for_version(bot, version, room.channel_type)
    _set_runtime_system_variables(
        runtime_state_before,
        bot=bot,
        channel_code=channel_code,
        room=room,
        participant_id=payload.participant_id or room.participant_id,
        participant_name=room.participant_name,
        message=payload.message,
    )
    runtime_state_after = runtime_state_before
    continuing_secondary = bool(runtime_state_before.get("__secondaryLlm"))
    secondary_handled = False
    selected_dialog = None
    score = 0.0
    bot_messages: list[ChannelMessage] = []
    form_response_handled = False
    hub_selection_handled = False
    structured_talk_input = _is_structured_talk_input(
        document,
        payload.source_talk_node_id,
        payload.message,
        channel_code,
        None if payload.source_talk_node_id is None else str(runtime_state_before.get("waitingNodeId") or None),
    )

    try:
        item = _append_runtime_event(
            runtime_state_before,
            level="info",
            event="channel.runtime.message_received",
            message="채널 Queue 메시지를 처리합니다.",
            data={
                "channel": channel,
                "roomId": str(room.id),
                "botId": str(bot.id),
                "botName": bot.name,
                "versionId": str(version.id),
                "participantId": payload.participant_id or room.participant_id,
                "messagePreview": _log_preview(payload.message, 500),
                "sourceTalkNodeId": payload.source_talk_node_id,
                "queueEventId": str(queue_event.id),
                "targetDialogId": payload.target_dialog_id,
                "systemName": payload.system_name,
            },
        )
        _log_runtime_event(item)
        interrupted_intent_transition = False
        if hub_call_rule_triggered:
            hub_selection_handled = True
            runtime_state = _initial_runtime_state_for_version(bot, version, room.channel_type)
            runtime_state["hubSelectionPending"] = True
            runtime_state["hubBotId"] = str(bot.id)
            _set_runtime_system_variables(
                runtime_state,
                bot=bot,
                channel_code=channel_code,
                room=room,
                participant_id=payload.participant_id or room.participant_id,
                participant_name=room.participant_name,
                message=payload.message,
            )
            runtime_outputs = [hub_selection_output(db, bot)]
            item = _append_runtime_event(
                runtime_state,
                level="info",
                event="channel.runtime.hub_call_rule_matched",
                message="Hub call rule routed the channel message to a bot hub.",
                data={
                    "hubBotId": str(bot.id),
                    "hubBotName": bot.name,
                    "ruleId": str(hub_call_rule_match[2].get("id") or ""),
                    "ruleName": str(hub_call_rule_match[2].get("name") or ""),
                    "messagePreview": _log_preview(payload.message, 500),
                },
            )
            _log_runtime_event(item)
            interrupted_intent_transition = True
        elif is_hub_bot(bot) and runtime_state_before.get("hubSelectionPending"):
            hub_selection_handled = True
            hub_bot = bot
            queue_event.parameter_json = {
                **parameter_json,
                "hub": {
                    "hubBotId": str(hub_bot.id),
                    "hubBotName": hub_bot.name,
                    "selectedBotId": None,
                    "selectedBotName": None,
                },
            }
            flag_modified(queue_event, "parameter_json")
            hub_config = hub_configuration(db, hub_bot)
            call_method = str(getattr(hub_config, "call_method", "button") or "button").strip().lower()
            allowed_candidate_ids = {
                str(value)
                for value in _as_list(runtime_state_before.get("hubCandidateBotIds"))
                if str(value).strip()
            }
            selected_member = resolve_hub_member(
                db,
                hub_bot,
                payload.message,
                allowed_bot_ids=allowed_candidate_ids or None,
            )
            selected_dialog_for_member = None
            selected_smalltalk = None
            natural_selected = False
            runtime_outputs: list[dict[str, Any]] = []
            if selected_member is None and call_method == "natural":
                hub_config, natural_candidates = _select_natural_hub_candidates(db, hub_bot, payload.message)
                if not natural_candidates:
                    runtime_outputs = [
                        {
                            "type": "text",
                            "text": str(
                                getattr(hub_config, "unrecognized_message", None)
                                or "\uc694\uccad\uc744 \uc774\ud574\ud558\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4. \ub2e4\uc2dc \ub9d0\uc500\ud574\uc8fc\uc138\uc694."
                            ).strip(),
                            "options": [],
                            "payload": {"hub": True, "callMethod": "natural"},
                        }
                    ]
                    runtime_state = runtime_state_before
                    item = _append_runtime_event(
                        runtime_state,
                        level="warning",
                        event="channel.runtime.hub_natural_unrecognized",
                        message="Hub natural call could not select a child bot.",
                        data={"messagePreview": _log_preview(payload.message, 500)},
                    )
                    _log_runtime_event(item)
                else:
                    top_candidate = natural_candidates[0]
                    similar_threshold = float(getattr(hub_config, "similar_intent_score", 0.85) or 0.85)
                    top_score = float(top_candidate["score"])
                    similar_candidates = [
                        candidate
                        for candidate in natural_candidates
                        if float(candidate["score"]) >= top_score * similar_threshold
                    ]
                    max_candidates = max(1, int(getattr(hub_config, "max_intent_candidates", 3) or 3))
                    similar_candidates = similar_candidates[:max_candidates]
                    if len(similar_candidates) > 1:
                        runtime_outputs = [
                            {
                                "type": "button",
                                "text": str(
                                    getattr(hub_config, "multiple_candidates_message", None)
                                    or "\uc6d0\ud558\ub294 \ubd07\uc744 \uc120\ud0dd\ud574\uc8fc\uc138\uc694."
                                ).strip(),
                                "options": [
                                    {
                                        "label": str(candidate["member"].display_name or candidate["bot"].name).strip()
                                        or candidate["bot"].name,
                                        "value": str(candidate["bot"].id),
                                        "botId": str(candidate["bot"].id),
                                    }
                                    for candidate in similar_candidates
                                ],
                                "payload": {"hub": True, "callMethod": "natural", "candidateSelection": True},
                            }
                        ]
                        runtime_state = runtime_state_before
                        runtime_state["hubCandidateBotIds"] = [str(candidate["bot"].id) for candidate in similar_candidates]
                        item = _append_runtime_event(
                            runtime_state,
                            level="info",
                            event="channel.runtime.hub_natural_candidates",
                            message="Hub natural call produced multiple child bot candidates.",
                            data={
                                "candidateBotIds": runtime_state["hubCandidateBotIds"],
                                "topScore": round(top_score * 100, 2),
                            },
                        )
                        _log_runtime_event(item)
                    else:
                        selected_candidate = top_candidate
                        selected_member = (
                            selected_candidate["bot"],
                            selected_candidate["version"],
                            selected_candidate["member"],
                        )
                        selected_dialog_for_member = selected_candidate["dialog"]
                        selected_smalltalk = selected_candidate.get("smalltalk")
                        natural_selected = True
            if selected_member is None and not runtime_outputs:
                runtime_outputs = [hub_selection_output(db, hub_bot)]
                runtime_state = runtime_state_before
                item = _append_runtime_event(
                    runtime_state,
                    level="warning",
                    event="channel.runtime.hub_selection_rejected",
                    message="Hub member selection did not match an active member.",
                    data={"messagePreview": _log_preview(payload.message, 500)},
                )
                _log_runtime_event(item)
            elif selected_member is not None:
                bot, version, hub_member = selected_member
                room.bot_id = bot.id
                room.bot_version_id = version.id
                queue_event.parameter_json = {
                    **parameter_json,
                    "hub": {
                        "hubBotId": str(hub_bot.id),
                        "hubBotName": hub_bot.name,
                        "selectedBotId": str(bot.id),
                        "selectedBotName": bot.name,
                    },
                }
                flag_modified(queue_event, "parameter_json")
                document = normalize_version_document(version.version_json)
                room_languages = _message_language_candidates(None, None, bot, room)
                default_messages = _load_default_messages(db, bot.organization_id, room_languages)
                runtime_state = _initial_runtime_state_for_version(bot, version, room.channel_type)
                variables = _as_dict(runtime_state.get("variables"))
                runtime_state["variables"] = variables
                _set_variable(variables, "_bot_hub_id", str(hub_bot.id))
                _set_variable(variables, "_bot_hub_name", hub_bot.name)
                runtime_state["hubSelectionPending"] = False
                runtime_state["hubBotId"] = str(hub_bot.id)
                runtime_state["hubSelectedBotId"] = str(bot.id)
                runtime_state.pop("hubCandidateBotIds", None)
                _set_runtime_system_variables(
                    runtime_state,
                    bot=bot,
                    channel_code=channel_code,
                    room=room,
                    participant_id=payload.participant_id or room.participant_id,
                    participant_name=room.participant_name,
                    message=payload.message,
                )
                if natural_selected and selected_smalltalk is not None:
                    runtime_outputs = [
                        {
                            "type": "text",
                            "text": str(selected_smalltalk.get("response") or ""),
                            "options": [],
                            "payload": {"hub": True, "smalltalk": True},
                        }
                    ]
                elif natural_selected and selected_dialog_for_member is not None:
                    runtime_outputs, runtime_state = _run_direct_dialog(
                        document,
                        runtime_state,
                        bot,
                        version,
                        selected_dialog_for_member,
                        {"$input": payload.message},
                        default_messages,
                        channel_code,
                        return_to_previous=False,
                    )
                else:
                    runtime_outputs, runtime_state = _run_runtime(
                        document,
                        runtime_state,
                        str(runtime_state.get("currentNodeId") or ""),
                        default_messages,
                        channel_code,
                    )
                item = _append_runtime_event(
                    runtime_state,
                    level="info",
                    event=(
                        "channel.runtime.hub_natural_member_selected"
                        if natural_selected
                        else "channel.runtime.hub_member_selected"
                    ),
                    message=(
                        "Hub natural call selected an active member bot."
                        if natural_selected
                        else "Hub selected an active member bot."
                    ),
                    data={
                        "hubBotId": str(hub_bot.id),
                        "selectedBotId": str(bot.id),
                        "selectedBotName": bot.name,
                        "displayName": str(hub_member.display_name or bot.name),
                        "intentId": str(selected_dialog_for_member.get("id") or "") if selected_dialog_for_member else "",
                    },
                )
                _log_runtime_event(item)
            interrupted_intent_transition = True
        if not hub_selection_handled and payload.target_dialog_id:
            selected_dialog = _direct_dialog_definition(document, payload.target_dialog_id)
            if selected_dialog is None:
                raise ValueError(f"대화를 찾을 수 없습니다: {payload.target_dialog_id}")
            score = 1.0
            runtime_outputs, runtime_state = _run_direct_dialog(
                document,
                runtime_state_before,
                bot,
                version,
                selected_dialog,
                payload.dialog_params,
                default_messages,
                channel_code,
                return_to_previous=not payload.direct_dialog_root,
            )
            interrupted_intent_transition = True
        elif not hub_selection_handled and payload.source_talk_node_id is None and not structured_talk_input:
            runtime_outputs, runtime_state, selected_dialog, interrupted_intent_transition = _interrupt_waiting_talk_with_intent(
                document,
                runtime_state_before,
                bot,
                version,
                payload.message,
                default_messages,
                channel_code,
            )
        if interrupted_intent_transition:
            form_response_handled = False
        else:
            form_response_handled, form_next_node_id = _store_form_result_variable(
                document,
                runtime_state_before,
                payload.source_talk_node_id,
                payload.message,
                channel_code,
                str(runtime_state_before.get("waitingNodeId") or None),
            )
            if form_response_handled:
                runtime_state_before["waitingNodeId"] = ""
                if form_next_node_id:
                    runtime_outputs, runtime_state = _run_runtime(document, runtime_state_before, form_next_node_id, default_messages, channel_code)
                else:
                    runtime_outputs, runtime_state = [], runtime_state_before
            else:
                runtime_outputs, runtime_state = _handle_runtime_message(
                    document,
                    runtime_state_before,
                    payload.message,
                    default_messages,
                    channel_code,
                    _button_selection_option(bot, version),
                )
        if continuing_secondary:
            secondary_result = _finish_secondary_runtime(document, runtime_state, bot, version, runtime_outputs, default_messages, channel_code)
            runtime_outputs, runtime_state = secondary_result.outputs, secondary_result.state
            secondary_handled = secondary_result.status != "unrecognized"
            if secondary_result.status == "unrecognized" and not runtime_outputs:
                runtime_outputs = [{"type": "text", "text": _reply_for_dialog(db, bot, None, room_languages), "options": []}]
        runtime_state_after = runtime_state
        room.metadata_json = runtime_state
        flag_modified(room, "metadata_json")

        if runtime_outputs:
            for output in runtime_outputs:
                bot_messages.append(
                    _add_message(
                        db,
                        room,
                        participant_id=str(bot.id),
                        participant_kind="bot",
                        participant_name=bot.name,
                        text=str(output.get("text") or ""),
                        message_type=str(output.get("type") or "text"),
                        payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
                    )
                )
        elif (not continuing_secondary) and (not interrupted_intent_transition) and _should_run_intent_fallback(
            runtime_outputs,
            form_response_handled,
            bool(runtime_state_before.get("waitingNodeId")),
            structured_talk_input,
        ) and _runtime_allows_general_intent_transition(document, runtime_state):
            score = 1.0
            intent_outputs, runtime_state, selected_dialog, handled_pre_nlu, nlu_message = _handle_pre_nlu_settings(
                document,
                runtime_state,
                bot,
                version,
                payload.message,
                default_messages,
                channel_code,
            )
            if not handled_pre_nlu:
                selected_dialog, score = _select_dialog_for_bot(
                    document,
                    bot,
                    version,
                    nlu_message,
                    prefer_exact_utterance=_exacting_matching_enabled(bot, version),
                )
            if selected_dialog:
                item = _append_runtime_event(
                    runtime_state,
                    level="info",
                    event="channel.runtime.intent_matched",
                    message="의도 인식 결과로 대화 흐름을 선택했습니다.",
                    data={
                        "intentId": str(selected_dialog.get("id") or ""),
                        "intentName": str(selected_dialog.get("name") or selected_dialog.get("displayName") or ""),
                        "intentScore": round(score * 100, 2),
                        "nluType": str(
                            _version_ai_config(bot, version).get("nlu_type")
                            or _version_ai_config(bot, version).get("nluType")
                            or "ml"
                        ),
                    },
                )
                _log_runtime_event(item)
            elif not handled_pre_nlu:
                item = _append_runtime_event(
                    runtime_state,
                    level="warning",
                    event="channel.runtime.intent_fallback",
                    message="의도를 찾지 못해 fallback 답변을 사용했습니다.",
                    data={"intentScore": round(score * 100, 2), "messagePreview": _log_preview(payload.message, 500)},
                )
                _log_runtime_event(item)

            if selected_dialog and not intent_outputs:
                runtime_state["intentFallbackCount"] = 0
                intent_graph = _graph_for_dialog(document, selected_dialog)
                start_node_id = _first_runtime_node_id(intent_graph) if intent_graph else None
                if intent_graph is not None and start_node_id:
                    _queue_runtime_return(document, runtime_state, selected_dialog)
                    runtime_state["__runtimeTransitionLocked"] = runtime_state.get("__runtimeTransitionLocked") is True or bool(
                        selected_dialog.get("transitionLocked") is True
                    )
                    runtime_state["__runtimeReturnBlocked"] = bool(selected_dialog.get("returnBlocked") is True)
                    runtime_state["graphId"] = str(intent_graph.get("id") or "")
                    runtime_state["dialogId"] = str(intent_graph.get("dialogId") or selected_dialog.get("id") or "")
                    runtime_state["waitingNodeId"] = ""
                    variables = _as_dict(runtime_state.get("variables"))
                    runtime_state["variables"] = variables
                    _set_variable(variables, "$input", nlu_message)
                    _prepare_answer_rag_variables(runtime_state, bot, version, nlu_message, selected_dialog)
                    intent_outputs, runtime_state = _run_runtime(document, runtime_state, start_node_id, default_messages, channel_code)
                    runtime_state_after = runtime_state
            elif not handled_pre_nlu:
                runtime_state["variables"] = dict(_as_dict(runtime_state.get("variables")))
                _set_variable(runtime_state["variables"], "$input", payload.message)
                secondary_result = _handle_local_intent_miss(
                    document,
                    runtime_state,
                    bot,
                    version,
                    default_messages,
                    channel_code,
                )
                intent_outputs, runtime_state = secondary_result.outputs, secondary_result.state
                secondary_handled = secondary_result.status != "unrecognized"
                runtime_state_after = runtime_state

            if intent_outputs:
                for output in intent_outputs:
                    bot_messages.append(
                        _add_message(
                            db,
                            room,
                            participant_id=str(bot.id),
                            participant_kind="bot",
                            participant_name=bot.name,
                            text=str(output.get("text") or ""),
                            message_type=str(output.get("type") or "text"),
                            payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
                        )
                    )
            elif not secondary_handled:
                bot_messages.append(
                    _add_message(
                        db,
                        room,
                        participant_id=str(bot.id),
                        participant_kind="bot",
                        participant_name=bot.name,
                        text=_reply_for_dialog(db, bot, selected_dialog, room_languages),
                    )
                )

        item = _append_runtime_event(
            runtime_state,
            level="info",
            event="channel.runtime.completed",
            message="채널 Queue 처리를 완료했습니다.",
            data={
                "queueEventId": str(queue_event.id),
                "botMessageCount": len(bot_messages),
                "dialogEnded": runtime_state.get("dialogEnded") is True,
                "sessionEnded": runtime_state.get("sessionEnded") is True,
                "completionReason": runtime_completion_reason(runtime_state),
            },
        )
        _log_runtime_event(item)
        room.metadata_json = runtime_state
        flag_modified(room, "metadata_json")
        _set_queue_status(
            queue_event,
            "completed",
            receive_status="completed",
            intent_name=str(selected_dialog.get("name") or selected_dialog.get("displayName") or "") if selected_dialog else None,
            result_json={
                "botMessageCount": len(bot_messages),
                "intentId": str(selected_dialog.get("id")) if selected_dialog else None,
                "intentScore": round(score * 100, 2),
                "dialogEnded": runtime_state.get("dialogEnded") is True,
                "sessionEnded": runtime_state.get("sessionEnded") is True,
                "completionReason": runtime_completion_reason(runtime_state),
                "runtimeEvents": _as_list(runtime_state.get("runtimeEvents")),
            },
        )
        if runtime_state.get("sessionEnded") is True:
            _archive_room_for_new_session(db, room, "end_session_immediately")
            _write_channel_audit_log(
                db,
                request,
                "channel.session.closed",
                room.id,
                {
                    "reason": "end_session_immediately",
                    "channel": channel,
                    "bot_id": str(bot.id),
                    "version_id": str(version.id),
                    "queue_event_id": str(queue_event.id),
                },
            )
    except Exception as error:
        item = _append_runtime_event(
            runtime_state_before,
            level="error",
            event="channel.runtime.exception",
            message="채널 Queue 처리 중 오류가 발생했습니다.",
            data={
                "channel": channel,
                "roomId": str(room.id),
                "botId": str(bot.id),
                "botName": bot.name,
                "versionId": str(version.id),
                "participantId": payload.participant_id or room.participant_id,
                "messagePreview": _log_preview(payload.message, 500),
                "errorType": type(error).__name__,
                "errorMessage": str(error),
                "queueEventId": str(queue_event.id),
            },
        )
        _log_runtime_event(item)
        logger.exception(
            "Channel queue runtime failed.",
            extra={
                "event": "channel.queue.runtime_exception",
                "extra_data": {
                    "channel": channel,
                    "room_id": str(room.id),
                    "bot_id": str(bot.id),
                    "bot_name": bot.name,
                    "version_id": str(version.id),
                    "queue_event_id": str(queue_event.id),
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            },
        )
        _set_queue_status(
            queue_event,
            "failed",
            receive_status="failed",
            error_message=str(error),
            result_json={
                "errorType": type(error).__name__,
                "errorMessage": str(error),
                "runtimeEvents": _as_list(runtime_state_before.get("runtimeEvents")),
            },
        )
        _write_channel_audit_log(
            db,
            request,
            "channel.runtime.error",
            room.id,
            {
                "channel": channel,
                "room_id": str(room.id),
                "bot_id": str(bot.id),
                "version_id": str(version.id),
                "queue_event_id": str(queue_event.id),
                "participant_id": payload.participant_id or room.participant_id,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        room.metadata_json = runtime_state_before
        flag_modified(room, "metadata_json")
        runtime_state_after = runtime_state_before
        bot_messages.append(
            _add_message(
                db,
                room,
                participant_id=str(bot.id),
                participant_kind="bot",
                participant_name=bot.name,
                text=_runtime_message(default_messages, "system_error", DEFAULT_MESSAGE_FALLBACKS["system_error"]),
            )
        )

    room.updated_at = datetime.now(timezone.utc)
    db.add(room)
    return {
        "roomId": str(room.id),
        "channelType": channel,
        "bot": {"id": str(bot.id), "name": bot.name},
        "activeVersion": {"id": str(version.id), "name": version.name, "versionNo": version.version_no},
        "queueEvent": {"id": str(queue_event.id), "status": queue_event.status, "receiveStatus": queue_event.receive_status},
        "botMessage": _serialize_message(bot_messages[-1]) if bot_messages else None,
        "botMessages": [_serialize_message(message) for message in bot_messages],
        "intent": {
            "id": str(selected_dialog.get("id")) if selected_dialog else None,
            "name": str(selected_dialog.get("name")) if selected_dialog else None,
            "score": round(score * 100, 2),
        },
        "runtime": {
            "dialogEnded": runtime_state_after.get("dialogEnded") is True,
            "sessionEnded": runtime_state_after.get("sessionEnded") is True,
            "completionReason": runtime_completion_reason(runtime_state_after),
                    "waitingNodeId": str(runtime_state_after.get("waitingNodeId") or "").strip() or None,
        },
    }


def _process_queued_channel_events(
    db: Session,
    request: Request,
    *,
    channel: str | None = None,
    organization_id: Any | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filters = [
        ChannelQueueEvent.status == "queued",
        ChannelQueueEvent.deleted_at.is_(None),
        ChannelQueueEvent.channel_type.not_in(NON_CHANNEL_QUEUE_TYPES),
        ChannelQueueEvent.operation_kind == "channel",
        (ChannelQueueEvent.room_id.is_(None)) | (ChannelQueueEvent.room_id.not_in(_persistent_room_ids())),
        (ChannelQueueEvent.room_id.is_(None)) | (ChannelQueueEvent.room_id.not_in(_credential_room_ids())),
    ]
    query = select(ChannelQueueEvent)
    if organization_id is not None:
        query = query.join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        filters.append(Bot.organization_id == organization_id)
    if channel:
        filters.append(ChannelQueueEvent.channel_type == channel)
    queue_events = db.scalars(
        query
        .where(*filters)
        .order_by(ChannelQueueEvent.created_at.asc())
        .limit(limit)
    ).all()
    return [
        _process_channel_queue_event(db, request, queue_event.channel_type, queue_event)
        for queue_event in queue_events
    ]


@router.get("/health")
def channel_health(request: Request) -> dict[str, object]:
    return success_response(
        request,
        {
            "status": "ok",
            "channels": sorted(SUPPORTED_CHANNELS),
            "details": _channel_health_summary(),
        },
    )


@router.post("/{channel_type}/connect")
def connect_channel(
    channel_type: str,
    payload: ChannelConnectRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        items = _list_active_channel_bots(db, channel, include_runtime_blocked=True)
        return success_response(
            request,
            {
                "channelType": channel,
                "connected": True,
                "clientId": payload.client_id or "webchat-client",
                "bots": [_serialize_bot(db, bot, version, group, channel) for bot, version, group in items],
            },
        )


@router.options("/{channel_type}/connect")
def connect_channel_options(channel_type: str, request: Request) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    return success_response(
        request,
        {
            "channelType": channel,
            "connected": True,
        },
    )

@router.get("/{channel_type}/bots")
def list_channel_bots(
    channel_type: str,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        items = _list_active_channel_bots(db, channel, include_runtime_blocked=True)
        return success_response(request, {"channelType": channel, "bots": [_serialize_bot(db, bot, version, group, channel) for bot, version, group in items]})


@router.options("/{channel_type}/bots")
def list_channel_bots_options(channel_type: str, request: Request) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    return success_response(request, {"channelType": channel, "bots": []})


@router.post("/kakao/webhook")
def kakao_webhook(
    payload: KakaoWebhookRequest,
    request: Request,
    bot_id: str | None = Query(default=None),
    x_aidot_channel_token: str | None = Header(default=None),
) -> dict[str, object]:
    normalized = _normalize_kakao_webhook_request(payload.model_dump(), bot_id_override=bot_id)
    log_context = {
        "event": "kakao.webhook.received",
        "channel": "kakao",
        "bot_id": normalized["bot_id"],
        "channel_user_id": normalized["channel_user_id"],
        "channel_room_id": normalized["channel_room_id"],
        "has_utterance": bool(normalized["utterance"]),
    }
    logger.info("Kakao webhook received.", extra=log_context)
    try:
        _verify_kakao_channel_request(normalized["bot_id"], x_aidot_channel_token)
        room_response = create_channel_room(
            "kakao",
            ChannelRoomCreateRequest(
                bot_id=normalized["bot_id"],
                client_room_id=normalized["channel_room_id"],
                participant_id=normalized["channel_user_id"],
                participant_name="카카오 사용자",
                use_configured_initial_messages=not bool(normalized["utterance"]),
            ),
            request,
            x_aidot_webchat_key=None,
        )
        room_data = _as_dict(room_response.get("data"))
        room_payload = _as_dict(room_data.get("room"))
        room_id = room_payload.get("id") or room_data.get("roomId") or room_data.get("sessionId")
        if not room_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Kakao 채널 대화방 생성 결과가 올바르지 않습니다.")

        initial_messages = [item for item in _as_list(room_data.get("messages")) if isinstance(item, dict)]
        room_bot = _as_dict(room_payload.get("bot"))
        room_initial_messages = [
            item for item in _as_list(room_bot.get("initialMessages")) if isinstance(item, dict)
        ]
        response_source_messages = room_initial_messages or initial_messages
        if not normalized["utterance"]:
            response_payload = _kakao_response_from_serialized_messages(response_source_messages)
        else:
            message_response = create_channel_room_message(
                "kakao",
                UUID(str(room_id)),
                ChannelMessageRequest(
                    message=normalized["utterance"],
                    participant_id=normalized["channel_user_id"],
                ),
                request,
                x_aidot_webchat_key=None,
            )
            message_data = _as_dict(message_response.get("data"))
            bot_messages = [item for item in _as_list(message_data.get("botMessages")) if isinstance(item, dict)]
            if not bot_messages:
                bot_message = _as_dict(message_data.get("botMessage"))
                if bot_message:
                    bot_messages = [bot_message]
            response_source_messages = bot_messages or initial_messages
            response_payload = _kakao_response_from_serialized_messages(response_source_messages)

        response_summary = _kakao_response_log_summary(response_payload, response_source_messages)
        logger.info(
            "Kakao webhook responded.",
            extra={
                **log_context,
                "event": "kakao.webhook.responded",
                "room_id": str(room_id),
                "output_types": response_summary["output_types"],
                "quick_reply_count": response_summary["quick_reply_count"],
                "fallback_used": response_summary["fallback_used"],
                "fallback_reasons": response_summary["fallback_reasons"],
            },
        )
        return response_payload
    except HTTPException as exc:
        logger.warning(
            "Kakao webhook rejected.",
            extra={
                **log_context,
                "event": "kakao.webhook.rejected",
                "status_code": exc.status_code,
                "detail": str(exc.detail or ""),
            },
        )
        raise
    except Exception:
        logger.exception("Kakao webhook failed.", extra={**log_context, "event": "kakao.webhook.failed"})
        raise


@router.post("/{channel_type}/rooms")
def create_channel_room(
    channel_type: str,
    payload: ChannelRoomCreateRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        existing_room = None
        if payload.client_room_id:
            existing_room = db.scalar(
                select(ChannelRoom).where(
                    ChannelRoom.channel_type == channel,
                    ChannelRoom.client_room_id == payload.client_room_id,
                    ChannelRoom.deleted_at.is_(None),
                )
            )
        if existing_room is not None:
            _assert_legacy_room_access(db, existing_room.id)
        bot, version, group = _get_active_bot_version(db, payload.bot_id, channel)
        request_languages = _message_language_candidates(
            payload.language,
            request.headers.get("accept-language"),
            bot,
            existing_room,
        )
        if existing_room is not None:
            existing_metadata = _as_dict(existing_room.metadata_json)
            existing_metadata["language"] = request_languages[0]
            existing_room.metadata_json = existing_metadata
            flag_modified(existing_room, "metadata_json")
            if existing_room.status != "open" or existing_room.bot_id != bot.id:
                _archive_room_for_new_session(db, existing_room, "replaced")
                _write_channel_audit_log(
                    db,
                    request,
                    "channel.session.closed",
                    existing_room.id,
                    {
                        "reason": "replaced",
                        "channel": channel,
                        "bot_id": str(existing_room.bot_id),
                    },
                )
                db.flush()
            elif existing_room.bot_version_id != version.id:
                previous_version_id = _apply_active_version_to_room(existing_room, bot, version, channel)
                _write_channel_audit_log(
                    db,
                    request,
                    "channel.session.version_applied",
                    existing_room.id,
                    {
                        "channel": channel,
                        "bot_id": str(bot.id),
                        "previous_version_id": previous_version_id,
                        "active_version_id": str(version.id),
                    },
                )
                db.flush()
            else:
                messages = _ensure_room_started(db, existing_room, bot, version, channel)
                db.commit()
                db.refresh(existing_room)
                for message in messages:
                    db.refresh(message)
                return success_response(
                    request,
                    {"room": _serialize_room(db, existing_room), "messages": [_serialize_message(message) for message in messages], "initialMessages": []},
                )
        if existing_room is not None and payload.client_room_id:
            db.flush()

        if existing_room is not None and existing_room.status == "open":
            messages = _ensure_room_started(db, existing_room, bot, version, channel)
            db.commit()
            db.refresh(existing_room)
            for message in messages:
                db.refresh(message)
            return success_response(
                request,
                {"room": _serialize_room(db, existing_room), "messages": [_serialize_message(message) for message in messages], "initialMessages": []},
            )

        runtime_state = _initial_runtime_state_for_version(bot, version, channel)
        runtime_state["language"] = request_languages[0]
        room = ChannelRoom(
            channel_type=channel,
            client_room_id=payload.client_room_id,
            bot_id=bot.id,
            bot_version_id=version.id,
            participant_id=payload.participant_id or "visitor",
            participant_name=payload.participant_name or "사용자",
            status="open",
            metadata_json=runtime_state,
        )
        db.add(room)
        db.flush()
        _set_runtime_system_variables(
            runtime_state,
            bot=bot,
            channel_code=_channel_template_code(channel),
            room=room,
            participant_id=room.participant_id,
            participant_name=room.participant_name,
        )
        room.metadata_json = runtime_state
        flag_modified(room, "metadata_json")

        bot_messages = []
        if payload.start_immediately and payload.use_configured_initial_messages:
            bot_messages = _add_configured_initial_messages(db, room, bot, version, channel)
        if payload.start_immediately and not bot_messages:
            bot_messages = _ensure_room_started(db, room, bot, version, channel, [])
        db.commit()
        db.refresh(room)
        for message in bot_messages:
            db.refresh(message)
        serialized_messages = [_serialize_message(message) for message in bot_messages]
        return success_response(request, {"room": _serialize_room(db, room), "messages": serialized_messages, "initialMessages": []})


@router.options("/{channel_type}/rooms")
def create_channel_room_options(channel_type: str, request: Request) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    return success_response(request, {"channelType": channel, "room": None})

@router.get("/{channel_type}/rooms")
def list_channel_rooms(
    channel_type: str,
    request: Request,
    participant_id: str | None = Query(default=None, max_length=120),
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        conditions = [ChannelRoom.channel_type == channel, ChannelRoom.deleted_at.is_(None), ChannelRoom.id.not_in(_persistent_room_ids()), ChannelRoom.id.not_in(_credential_room_ids())]
        if participant_id:
            conditions.append(ChannelRoom.participant_id == participant_id)
        rooms = db.scalars(
            select(ChannelRoom)
            .where(*conditions)
            .order_by(ChannelRoom.updated_at.desc())
        ).all()
        return success_response(request, {"channelType": channel, "rooms": [_serialize_room(db, room) for room in rooms]})


@router.get("/{channel_type}/rooms/{room_id}")
def get_channel_room(
    channel_type: str,
    room_id: UUID,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        _assert_legacy_room_access(db, room_id)
        room = db.scalar(
            select(ChannelRoom).where(
                ChannelRoom.id == room_id,
                ChannelRoom.channel_type == channel,
                ChannelRoom.deleted_at.is_(None),
            )
        )
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채팅방을 찾을 수 없습니다.")
        messages = db.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.room_id == room.id, ChannelMessage.deleted_at.is_(None))
            .order_by(ChannelMessage.created_at.asc())
        ).all()
        bot = db.get(Bot, room.bot_id)
        version = db.get(BotVersion, room.bot_version_id)
        if room.status == "open" and bot is not None and version is not None:
            messages = _ensure_room_started(db, room, bot, version, channel, list(messages))
            db.commit()
            db.refresh(room)
            for message in messages:
                db.refresh(message)
        return success_response(request, {"room": _serialize_room(db, room), "messages": [_serialize_message(message) for message in messages]})


@router.options("/{channel_type}/rooms/{room_id}")
def get_channel_room_options(
    channel_type: str,
    room_id: UUID,
    request: Request,
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    return success_response(
        request,
        {
            "channelType": channel,
            "roomId": str(room_id),
            "exists": False,
        },
    )


@router.delete("/{channel_type}/rooms/{room_id}")
def delete_channel_room(
    channel_type: str,
    room_id: UUID,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        _assert_legacy_room_access(db, room_id)
        room = db.scalar(
            select(ChannelRoom).where(
                ChannelRoom.id == room_id,
                ChannelRoom.channel_type == channel,
                ChannelRoom.deleted_at.is_(None),
            )
        )
        if room is not None:
            _archive_room_for_new_session(db, room, "client_deleted")
            _write_channel_audit_log(
                db,
                request,
                "channel.session.closed",
                room.id,
                {"reason": "client_deleted", "channel": channel, "room_id": str(room.id)},
            )
            db.commit()
        return success_response(request, {"roomId": str(room_id), "deleted": True})


@router.post("/{channel_type}/rooms/{room_id}/messages")
def create_channel_room_message(
    channel_type: str,
    room_id: UUID,
    payload: ChannelMessageRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    channel_code = _channel_template_code(channel)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db, _shared_room_execution_db(db):
        _assert_legacy_room_access(db, room_id)
        room = db.scalar(
            select(ChannelRoom).where(
                ChannelRoom.id == room_id,
                ChannelRoom.channel_type == channel,
                ChannelRoom.status == "open",
                ChannelRoom.deleted_at.is_(None),
            )
        )
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채팅방을 찾을 수 없습니다.")
        bot = db.get(Bot, room.bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="채팅방의 봇 정보를 찾을 수 없습니다.")
        version = db.get(BotVersion, room.bot_version_id)
        if version is None or bot.active_version_id != room.bot_version_id or version.status != "active":
            _, version, _ = _get_active_bot_version(db, str(bot.id), channel)
            previous_version_id = _apply_active_version_to_room(room, bot, version, channel)
            _write_channel_audit_log(
                db,
                request,
                "channel.session.version_applied",
                room.id,
                {
                    "channel": channel,
                    "bot_id": str(bot.id),
                    "previous_version_id": previous_version_id,
                    "active_version_id": str(version.id),
                },
            )
        if _requires_botstation_connection(channel):
            _ensure_botstation_connection(db, bot, version, channel)
        _ensure_runtime_supported(bot, version)
        message_languages = _message_language_candidates(
            payload.language,
            request.headers.get("accept-language"),
            bot,
            room,
        )
        room_metadata = _as_dict(room.metadata_json)
        room_metadata["language"] = message_languages[0]
        room.metadata_json = room_metadata
        flag_modified(room, "metadata_json")

        user_message = _add_message(
            db,
            room,
            participant_id=payload.participant_id or room.participant_id,
            participant_kind="user",
            participant_name=(_shared_turn_capability.get() or {}).get("participant_name") or room.participant_name,
            text=payload.message,
        )
        db.flush()
        queue_event = _create_queue_event(db, room, bot, version, user_message, payload)
        db.flush()
        if payload.defer_processing:
            db.commit()
            db.refresh(user_message)
            db.refresh(queue_event)
            return success_response(
                request,
                {
                    "roomId": str(room.id),
                    "channelType": channel,
                    "queued": True,
                    "bot": {"id": str(bot.id), "name": bot.name},
                    "activeVersion": {"id": str(version.id), "name": version.name, "versionNo": version.version_no},
                    "queueEvent": {"id": str(queue_event.id), "status": queue_event.status, "receiveStatus": queue_event.receive_status},
                    "userMessage": _serialize_message(user_message),
                    "botMessage": None,
                    "botMessages": [],
                },
            )
        _set_queue_status(queue_event, "processing", receive_status="processing")

        document = normalize_version_document(version.version_json)
        default_messages = _load_default_messages(db, bot.organization_id, message_languages)
        runtime_state_before = _as_dict(room.metadata_json) or _initial_runtime_state_for_version(bot, version, room.channel_type)
        if _shared_turn_capability.get() is not None:
            # 이번 실행의 실제 대기를 이전 질문의 종료 flag와 분리한다.
            runtime_state_before["dialogEnded"] = False
            runtime_state_before["sessionEnded"] = False
        _reset_stalled_waiting_talk_state(document, runtime_state_before, channel_code)
        runtime_state_snapshot = deepcopy(runtime_state_before)
        _set_runtime_system_variables(
            runtime_state_before,
            bot=bot,
            channel_code=channel_code,
            room=room,
            participant_id=payload.participant_id or room.participant_id,
            participant_name=(_shared_turn_capability.get() or {}).get("participant_name") or room.participant_name,
            message=payload.message,
        )
        runtime_state_after = runtime_state_before
        continuing_secondary = bool(runtime_state_before.get("__secondaryLlm"))
        secondary_handled = False
        selected_dialog = None
        score = 0.0
        bot_messages: list[ChannelMessage] = []
        form_response_handled = False
        structured_talk_input = _is_structured_talk_input(
            document,
            payload.source_talk_node_id,
            payload.message,
            channel_code,
            None if payload.source_talk_node_id is None else str(runtime_state_before.get("waitingNodeId") or None),
        )

        shared_turn = _shared_turn_capability.get()
        if shared_turn is not None:
            shared_turn["queue_event_id"] = queue_event.id
            queue_event.parameter_json = {**_as_dict(queue_event.parameter_json), "sharedTurnGeneration": shared_turn["generation"]}
            flag_modified(queue_event, "parameter_json")
            # 입력/processing을 영속화한 뒤 외부 실행 전 transaction을 종료한다.
            db.commit()
            shared_turn["fenced"] = False
            shared_turn["protected"] = {}
            shared_turn["written_history"] = None

        try:
            item = _append_runtime_event(
                runtime_state_before,
                level="info",
                event="channel.runtime.message_received",
                message="채널 메시지를 수신했습니다.",
                data={
                    "channel": channel,
                    "roomId": str(room.id),
                    "botId": str(bot.id),
                    "botName": bot.name,
                    "versionId": str(version.id),
                    "participantId": payload.participant_id or room.participant_id,
                    "messagePreview": _log_preview(payload.message, 500),
                    "sourceTalkNodeId": payload.source_talk_node_id,
                    "queueEventId": str(queue_event.id),
                },
            )
            _log_runtime_event(item)
            interrupted_intent_transition = False
            if payload.target_dialog_id:
                selected_dialog = _direct_dialog_definition(document, payload.target_dialog_id)
                if selected_dialog is None:
                    raise ValueError(f"대화를 찾을 수 없습니다: {payload.target_dialog_id}")
                score = 1.0
                runtime_outputs, runtime_state = _run_direct_dialog(
                    document,
                    runtime_state_before,
                    bot,
                    version,
                    selected_dialog,
                    payload.dialog_params,
                    default_messages,
                    channel_code,
                    return_to_previous=not payload.direct_dialog_root,
                )
                interrupted_intent_transition = True
            elif payload.source_talk_node_id is None and not structured_talk_input:
                runtime_outputs, runtime_state, selected_dialog, interrupted_intent_transition = _interrupt_waiting_talk_with_intent(
                    document,
                    runtime_state_before,
                    bot,
                    version,
                    payload.message,
                    default_messages,
                    channel_code,
                )
            if interrupted_intent_transition:
                form_response_handled = False
            else:
                form_response_handled, form_next_node_id = _store_form_result_variable(
                    document,
                    runtime_state_before,
                    payload.source_talk_node_id,
                    payload.message,
                    channel_code,
                    str(runtime_state_before.get("waitingNodeId") or None),
                )
                if form_response_handled:
                    runtime_state_before["waitingNodeId"] = ""
                    if form_next_node_id:
                        runtime_outputs, runtime_state = _run_runtime(document, runtime_state_before, form_next_node_id, default_messages, channel_code)
                    else:
                        runtime_outputs, runtime_state = [], runtime_state_before
                else:
                    runtime_outputs, runtime_state = _handle_runtime_message(
                        document,
                        runtime_state_before,
                        payload.message,
                        default_messages,
                        channel_code,
                        _button_selection_option(bot, version),
                    )
            if continuing_secondary:
                secondary_result = _finish_secondary_runtime(document, runtime_state, bot, version, runtime_outputs, default_messages, channel_code)
                runtime_outputs, runtime_state = secondary_result.outputs, secondary_result.state
                secondary_handled = secondary_result.status != "unrecognized"
                if secondary_result.status == "unrecognized" and not runtime_outputs:
                    runtime_outputs = [{"type": "text", "text": _reply_for_dialog(db, bot, None, message_languages), "options": []}]
            runtime_state_after = runtime_state
            room.metadata_json = runtime_state
            flag_modified(room, "metadata_json")

            if runtime_outputs:
                for output in runtime_outputs:
                    bot_messages.append(
                        _add_message(
                            db,
                            room,
                            participant_id=str(bot.id),
                            participant_kind="bot",
                            participant_name=bot.name,
                            text=str(output.get("text") or ""),
                            message_type=str(output.get("type") or "text"),
                            payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
                        )
                    )
            elif (not continuing_secondary) and (not interrupted_intent_transition) and _should_run_intent_fallback(
                runtime_outputs,
                form_response_handled,
                bool(runtime_state_before.get("waitingNodeId")),
                structured_talk_input,
            ) and _runtime_allows_general_intent_transition(document, runtime_state):
                score = 1.0
                intent_outputs, runtime_state, selected_dialog, handled_pre_nlu, nlu_message = _handle_pre_nlu_settings(
                    document,
                    runtime_state,
                    bot,
                    version,
                    payload.message,
                    default_messages,
                    channel_code,
                )
                if not handled_pre_nlu:
                    selected_dialog, score = _select_dialog_for_bot(
                        document,
                        bot,
                        version,
                        nlu_message,
                        prefer_exact_utterance=_exacting_matching_enabled(bot, version),
                    )
                if selected_dialog:
                    item = _append_runtime_event(
                        runtime_state,
                        level="info",
                        event="channel.runtime.intent_matched",
                        message="의도 인식 결과로 대화 흐름을 선택했습니다.",
                        data={
                            "intentId": str(selected_dialog.get("id") or ""),
                            "intentName": str(selected_dialog.get("name") or selected_dialog.get("displayName") or ""),
                            "intentScore": round(score * 100, 2),
                            "nluType": str(
                                _version_ai_config(bot, version).get("nlu_type")
                                or _version_ai_config(bot, version).get("nluType")
                                or "ml"
                            ),
                        },
                    )
                    _log_runtime_event(item)
                elif not handled_pre_nlu:
                    item = _append_runtime_event(
                        runtime_state,
                        level="warning",
                        event="channel.runtime.intent_fallback",
                        message="의도를 찾지 못해 fallback 답변을 사용했습니다.",
                        data={"intentScore": round(score * 100, 2), "messagePreview": _log_preview(payload.message, 500)},
                    )
                    _log_runtime_event(item)

                if selected_dialog and not intent_outputs:
                    runtime_state["intentFallbackCount"] = 0
                    intent_graph = _graph_for_dialog(document, selected_dialog)
                    start_node_id = _first_runtime_node_id(intent_graph) if intent_graph else None
                    if intent_graph is not None and start_node_id:
                        _queue_runtime_return(document, runtime_state, selected_dialog)
                        runtime_state["__runtimeTransitionLocked"] = runtime_state.get("__runtimeTransitionLocked") is True or bool(
                            selected_dialog.get("transitionLocked") is True
                        )
                        runtime_state["__runtimeReturnBlocked"] = bool(selected_dialog.get("returnBlocked") is True)
                        runtime_state["graphId"] = str(intent_graph.get("id") or "")
                        runtime_state["dialogId"] = str(intent_graph.get("dialogId") or selected_dialog.get("id") or "")
                        runtime_state["waitingNodeId"] = ""
                        variables = _as_dict(runtime_state.get("variables"))
                        runtime_state["variables"] = variables
                        _set_variable(variables, "$input", nlu_message)
                        _prepare_answer_rag_variables(runtime_state, bot, version, nlu_message, selected_dialog)
                        intent_outputs, runtime_state = _run_runtime(document, runtime_state, start_node_id, default_messages, channel_code)
                        runtime_state_after = runtime_state
                elif not handled_pre_nlu:
                    runtime_state["variables"] = dict(_as_dict(runtime_state.get("variables")))
                    _set_variable(runtime_state["variables"], "$input", payload.message)
                    secondary_result = _handle_local_intent_miss(
                        document,
                        runtime_state,
                        bot,
                        version,
                        default_messages,
                        channel_code,
                    )
                    intent_outputs, runtime_state = secondary_result.outputs, secondary_result.state
                    secondary_handled = secondary_result.status != "unrecognized"
                    runtime_state_after = runtime_state

                if intent_outputs:
                    for output in intent_outputs:
                        bot_messages.append(
                            _add_message(
                                db,
                                room,
                                participant_id=str(bot.id),
                                participant_kind="bot",
                                participant_name=bot.name,
                                text=str(output.get("text") or ""),
                                message_type=str(output.get("type") or "text"),
                                payload_json={"options": output.get("options") or [], **_as_dict(output.get("payload"))},
                            )
                        )
                elif not secondary_handled:
                    reply = _reply_for_dialog(db, bot, selected_dialog, message_languages)
                    bot_messages.append(
                        _add_message(
                            db,
                            room,
                            participant_id=str(bot.id),
                            participant_kind="bot",
                            participant_name=bot.name,
                            text=reply,
                        )
                    )
            item = _append_runtime_event(
                runtime_state,
                level="info",
                event="channel.runtime.completed",
                message="채널 메시지 처리를 완료했습니다.",
                data={
                    "queueEventId": str(queue_event.id),
                    "botMessageCount": len(bot_messages),
                    "dialogEnded": runtime_state.get("dialogEnded") is True,
                    "sessionEnded": runtime_state.get("sessionEnded") is True,
                    "completionReason": runtime_completion_reason(runtime_state),
                },
            )
            _log_runtime_event(item)
            room.metadata_json = runtime_state
            flag_modified(room, "metadata_json")
            _set_queue_status(
                queue_event,
                "completed",
                receive_status="completed",
                intent_name=str(selected_dialog.get("name") or selected_dialog.get("displayName") or "") if selected_dialog else None,
                result_json={
                    "botMessageCount": len(bot_messages),
                    "intentId": str(selected_dialog.get("id")) if selected_dialog else None,
                    "intentScore": round(score * 100, 2),
                    "dialogEnded": runtime_state.get("dialogEnded") is True,
                    "sessionEnded": runtime_state.get("sessionEnded") is True,
                    "completionReason": runtime_completion_reason(runtime_state),
                    "runtimeEvents": _as_list(runtime_state.get("runtimeEvents")),
                },
            )
            if runtime_state.get("sessionEnded") is True:
                # 출력 없는 End에서도 생명주기 변경 전에 세대를 검증한다.
                _fence_shared_room_write(db, room)
                _archive_room_for_new_session(db, room, "end_session_immediately")
                _write_channel_audit_log(
                    db,
                    request,
                    "channel.session.closed",
                    room.id,
                    {
                        "reason": "end_session_immediately",
                        "channel": channel,
                        "bot_id": str(bot.id),
                        "version_id": str(version.id),
                        "queue_event_id": str(queue_event.id),
                    },
                )
        except Exception as error:
            item = _append_runtime_event(
                runtime_state_before,
                level="error",
                event="channel.runtime.exception",
                message="채널 런타임 처리 중 오류가 발생했습니다.",
                data={
                    "channel": channel,
                    "roomId": str(room.id),
                    "botId": str(bot.id),
                    "botName": bot.name,
                    "versionId": str(version.id),
                    "participantId": payload.participant_id or room.participant_id,
                    "messagePreview": _log_preview(payload.message, 500),
                    "errorType": type(error).__name__,
                    "errorMessage": str(error),
                    "queueEventId": str(queue_event.id),
                },
            )
            _log_runtime_event(item)
            logger.exception(
                "Channel runtime failed.",
                extra={
                    "event": "channel.runtime.exception",
                    "extra_data": {
                        "channel": channel,
                        "room_id": str(room.id),
                        "bot_id": str(bot.id),
                        "bot_name": bot.name,
                        "version_id": str(version.id),
                        "participant_id": payload.participant_id or room.participant_id,
                        "message_preview": _log_preview(payload.message, 500),
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    },
                },
            )
            _set_queue_status(
                queue_event,
                "failed",
                receive_status="failed",
                error_message=str(error),
                result_json={
                    "errorType": type(error).__name__,
                    "errorMessage": str(error),
                    "runtimeEvents": _as_list(runtime_state_before.get("runtimeEvents")),
                },
            )
            _write_channel_audit_log(
                db,
                request,
                "channel.runtime.error",
                room.id,
                {
                    "channel": channel,
                    "room_id": str(room.id),
                    "bot_id": str(bot.id),
                    "version_id": str(version.id),
                    "queue_event_id": str(queue_event.id),
                    "participant_id": payload.participant_id or room.participant_id,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            room.metadata_json = runtime_state_snapshot
            flag_modified(room, "metadata_json")
            runtime_state_after = runtime_state_snapshot
            bot_messages.append(
                _add_message(
                    db,
                    room,
                    participant_id=str(bot.id),
                    participant_kind="bot",
                    participant_name=bot.name,
                    text=_runtime_message(default_messages, "system_error", DEFAULT_MESSAGE_FALLBACKS["system_error"]),
                )
            )
        if shared_turn is not None:
            from app.services.room_turn import hold_room_turn_for_input, release_room_turn
            if queue_event.status == "failed":
                # 실패한 질문의 대기/변수를 다음 참가자가 이어받지 않는다.
                previous = _as_dict(room.metadata_json)
                fresh = _initial_runtime_state_for_version(bot, version, channel)
                for key in ("__webchatAccess", "__roomTurn", "conversationHistory", "language", "runtimeEvents"):
                    if key in previous:
                        fresh[key] = previous[key]
                fresh["dialogEnded"] = True
                room.metadata_json = fresh
                flag_modified(room, "metadata_json")
                runtime_state_after = fresh
                _set_queue_status(queue_event, "failed", receive_status=queue_event.receive_status,
                    result_json=queue_event.result_json, error_message=queue_event.error_message)
            _fence_shared_room_write(db, room)
            db.flush()
            waiting_input = bool(str(runtime_state_after.get("waitingNodeId") or "").strip())
            if queue_event.status == "failed" or runtime_state_after.get("sessionEnded") is True or not waiting_input:
                release_room_turn(db, room.id, shared_turn["actor_id"], shared_turn["generation"],
                    "error" if queue_event.status == "failed" else "session_ended" if runtime_state_after.get("sessionEnded") is True else "dialog_ended")
            else:
                hold_room_turn_for_input(db, room.id, shared_turn["actor_id"], shared_turn["generation"])
        room.updated_at = datetime.now(timezone.utc)
        db.add(room)
        db.commit()
        db.refresh(user_message)
        for bot_message in bot_messages:
            db.refresh(bot_message)

        serialized_bot_messages = [_serialize_message(message) for message in bot_messages]
        return success_response(
            request,
            {
                "roomId": str(room.id),
                "channelType": channel,
                "bot": {"id": str(bot.id), "name": bot.name},
                "activeVersion": {"id": str(version.id), "name": version.name, "versionNo": version.version_no},
                "queueEvent": {"id": str(queue_event.id), "status": queue_event.status, "receiveStatus": queue_event.receive_status},
                "userMessage": _serialize_message(user_message),
                "botMessage": serialized_bot_messages[-1] if serialized_bot_messages else None,
                "botMessages": serialized_bot_messages,
                "intent": {
                    "id": str(selected_dialog.get("id")) if selected_dialog else None,
                    "name": str(selected_dialog.get("name")) if selected_dialog else None,
                    "score": round(score * 100, 2),
                },
                "runtime": {
                    "dialogEnded": runtime_state_after.get("dialogEnded") is True,
                    "sessionEnded": runtime_state_after.get("sessionEnded") is True,
                    "completionReason": runtime_completion_reason(runtime_state_after),
                    "waitingNodeId": str(runtime_state_after.get("waitingNodeId") or "").strip() or None,
                },
            },
        )


@router.options("/{channel_type}/rooms/{room_id}/messages")
def create_channel_room_message_options(
    channel_type: str,
    room_id: UUID,
    request: Request,
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    return success_response(
        request,
        {
            "channelType": channel,
            "roomId": str(room_id),
            "messageAccepted": True,
        },
    )


@router.post("/{channel_type}/queues/process")
def process_channel_queue_events(
    channel_type: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, object]:
    channel = _ensure_channel(channel_type)
    _verify_channel_key(x_aidot_webchat_key)
    with SessionLocal() as db:
        results = _process_queued_channel_events(db, request, channel=channel, limit=limit)
        db.commit()
        return success_response(
            request,
            {
                "channelType": channel,
                "processed": len(results),
                "items": results,
            },
        )
