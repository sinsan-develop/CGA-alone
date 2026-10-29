from __future__ import annotations

import base64
import binascii
import io
import json
import re
import random
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from uuid import UUID, uuid4
from copy import deepcopy

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, object_session, defer

from app.api.deps import get_current_user, get_db
from app.core.cache import cache_aside_json, purge_cache_pattern
from app.core.config import ROOT_DIR, settings
from app.core.logging import get_logger
from app.core.responses import success_response
from app.core.version_documents import (
    build_default_version_document,
    build_version_asset_counts,
    is_system_version_asset,
    normalize_version_document,
)
from app.core.version_storage import sync_version_dialog_split_tables
from app.models import (
    AdminChannel,
    AdminTemplate,
    AuditLog,
    Bot,
    BotHub,
    BotHubMember,
    BotVersion,
    ChannelMessage,
    ChannelQueueEvent, ChannelLlmVersionGuard,
    ChannelRoom,
    EditLock,
    Group,
    Role,
    User,
    UserRole,
    VersionDialogAsset,
    VersionDialogFlowGraph,
)
from app.schemas.bot import (
    BotCreateRequest,
    BotUpdateRequest,
    LlmIntentConfigureRequest,
    LlmIntentTestRequest,
    MlIntentConfigureRequest,
    MlIntentTokenizeRequest,
    RagAnswerConfigureRequest,
    SemanticIntentConfigureRequest,
    VersionConfigureUpdateRequest,
    VersionCreateRequest,
    VersionDialogFlowUpdateRequest,
    VersionDocumentItemsUpdateRequest,
    VersionNluTrainRequest,
    VersionRetrainingUpdateRequest,
    VersionUpdateRequest,
)
from app.services.license_policy import assert_license_allows_creation, count_registered_apis, count_registered_apis_with_version_override
from app.services.bot_ai_policy import training_block_reason
from app.services.aidot_package_compatibility import (
    aidot_bot_package_to_version_document,
    aidot_package_summary,
    version_document_to_aidot_bot_package,
)
from app.services.version_pins import (version_mutation, admit_version_write, admit_bot_version_writes,
    assert_active_version_write, mark_external_version_write, handoff_version_write, register_sample_version,
    claim_training_version_write, training_version_write, quarantine_interrupted_training,
    MutationBusy, PinConflict, retains_persistent_history, remove_unused_version_guard)
from app.models import ChannelLlmBinding, LlmBudgetAccount, LlmBudgetReservation, AdminLicense
from app.schemas.persistent_llm import (TurnScope, BudgetQuote, UsageEvidence, PersistentRoomStartRequest,
    PersistentTurnRequest, PersistentAckRequest, PersistentResetRequest, PersistentCloseRequest, PersistentRevokeRequest)
from app.services.persistent_llm import PersistentLlmService, TurnConflict
from app.services.openai_conversations import OpenAIConversations, BoundedJsonTransport, payload_hash
from app.services.llm_budget import BudgetConflict, release_presubmit_budget, hold_unknown_budget
from app.services.llm_client import LlmChatClient, LlmClientError, resolve_llm_provider_config
from app.services.llm_intent import classify_intent_with_llm, configure_intents_with_llm
from app.services.llm_nlu import build_llm_nlu_training_snapshot, classify_intent_with_llm_snapshot
from app.services.nlu.deep_learning_lite import (
    build_learning_token_context,
    calculate_nlu_evaluation,
    score_deep_learning_lite_model,
    configure_intents_with_deep_learning_lite,
    get_deep_learning_lite_model_manifest,
    tokenize_texts_for_deep_learning_lite,
    train_and_save_deep_learning_lite_model,
)
from app.services.scenario_validation import attach_scenario_validation, save_blocking_scenario_items, scenario_validation_error_detail, scenario_validation_from_version
from app.services.vector_search import (
    AnswerVectorSearchClient,
    IntentVectorSearchClient,
    VectorSearchError,
    answer_vector_config,
    intent_vector_config,
)


router = APIRouter(prefix="/bots", tags=["bots"])
logger = get_logger("aidot.nlu_training")
SEMANTIC_NLU_TYPES = {"semantic", "semantic_vector", "semantic_external"}
API_WRITE_ROLE_CODES = {"operation_manager", "system_manager", "it_admin"}
VERSION_OPERATING_ROLE_CODES = {"operation_manager", "system_manager", "it_admin"}
NLU_TRAINING_QUEUE_CHANNEL = "training"
NLU_TRAINING_QUEUE_OPERATION = "nlu.train"
PROFILE_IMAGE_MAX_BYTES = 2 * 1024 * 1024
PROFILE_IMAGE_DATA_URL_PATTERN = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE)
PROFILE_IMAGE_SIGNATURES = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/webp": b"RIFF",
}
PROFILE_IMAGE_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _remove_profile_images(bot_id: UUID) -> None:
    directory = ROOT_DIR / "storage" / "bot-images" / "profiles"
    for extension in set(PROFILE_IMAGE_EXTENSIONS.values()):
        (directory / f"{bot_id}.{extension}").unlink(missing_ok=True)


def _store_profile_image(bot_id: UUID, value: str | None) -> str | None:
    if not value:
        return None
    matched = PROFILE_IMAGE_DATA_URL_PATTERN.fullmatch(value.strip())
    if not matched:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="프로필 이미지는 PNG, JPEG 또는 WEBP 파일만 사용할 수 있습니다.")
    mime_type = matched.group(1).lower()
    encoded = re.sub(r"\s+", "", matched.group(2))
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="프로필 이미지 데이터 형식이 올바르지 않습니다.") from error
    if not content or len(content) > PROFILE_IMAGE_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="프로필 이미지는 2MB 이하만 사용할 수 있습니다.")
    signature = PROFILE_IMAGE_SIGNATURES[mime_type]
    if not content.startswith(signature) or (mime_type == "image/webp" and content[8:12] != b"WEBP"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="프로필 이미지 파일 형식이 올바르지 않습니다.")
    relative_path = Path("profiles") / f"{bot_id}.{PROFILE_IMAGE_EXTENSIONS[mime_type]}"
    target = ROOT_DIR / "storage" / "bot-images" / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    for extension in set(PROFILE_IMAGE_EXTENSIONS.values()):
        stale_target = target.parent / f"{bot_id}.{extension}"
        if stale_target != target:
            stale_target.unlink(missing_ok=True)
    return "/files/bot-images/" + relative_path.as_posix()


def _get_user_role_codes(db: Session, user_id: UUID) -> list[str]:
    return list(
        db.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, Role.deleted_at.is_(None))
        ).all()
    )


def _require_api_write_user(db: Session, current_user: User) -> None:
    role_codes = set(_get_user_role_codes(db, current_user.id))
    if not role_codes.intersection(API_WRITE_ROLE_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API 수정 권한이 필요합니다.",
        )


def _require_version_operating_user(db: Session, current_user: User) -> None:
    role_codes = set(_get_user_role_codes(db, current_user.id))
    if not role_codes.intersection(VERSION_OPERATING_ROLE_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="운영 버전 변경 권한이 필요합니다.",
        )


def _serialize_talk_template(template: AdminTemplate, channel_name: str) -> dict[str, object]:
    item_types = [item.strip() for item in template.item_types.split(",") if item.strip()]
    return {
        "id": str(template.id),
        "channel_code": template.channel_code,
        "channel_name": channel_name,
        "name": template.name,
        "renderer_type": template.renderer_type,
        "item_count": len(item_types),
        "item_types": template.item_types,
        "description": template.description,
        "status": template.status,
        "status_label": "사용" if template.status == "active" else "미사용",
        "updated_at": _iso(template.updated_at),
        "data_json": template.data_json or {},
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _build_bot_json(payload: BotCreateRequest) -> dict[str, object]:
    return {
        "bot_kind": payload.bot_kind,
        "bot_mode": payload.bot_mode,
        "profile_key": payload.profile_key,
        "language": payload.language,
        "nlu_engine": payload.nlu_engine,
        "nlu_type": payload.nlu_type,
        "nlu_model": payload.nlu_model,
        "answer_mode": payload.answer_mode,
        "llm_provider": payload.llm_provider,
        "llm_model": payload.llm_model,
        "llm_base_url": payload.llm_base_url.strip() if payload.llm_base_url else None,
        "vector_connections": payload.vector_connections or {},
        "configuration_scoring": payload.configuration_scoring or {},
        "introduction": payload.introduction.strip() if payload.introduction else None,
    }


AI_CONFIG_KEYS = {
    "language",
    "nlu_engine",
    "nlu_type",
    "nlu_model",
    "answer_mode",
    "llm_provider",
    "llm_model",
    "llm_base_url",
    "vector_connections",
    "configuration_scoring",
}

LOCKED_AI_CONFIG_KEYS = {"language", "nlu_type", "answer_mode"}
LOCKED_AI_CONFIG_MESSAGE = "언어, NLU 방식, 답변 방식은 봇 생성 시 고정됩니다. 모델은 학습 전까지 변경할 수 있고, 학습 완료 후에는 변경할 수 없습니다."


def _merge_json_dict(base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in patch.items():
        current_value = merged.get(key)
        if isinstance(current_value, dict) and isinstance(value, dict):
            merged[key] = _merge_json_dict(current_value, value)
            continue
        merged[key] = value
    return merged


def _settings_meta_string(source: object, *keys: str) -> str:
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _apply_smalltalk_item_metadata(
    existing_settings: dict[str, object],
    settings_json: dict[str, object],
    current_user: User,
    now: datetime,
) -> dict[str, object]:
    smalltalk = settings_json.get("smalltalk")
    if not isinstance(smalltalk, dict):
        return settings_json
    items = smalltalk.get("items")
    if not isinstance(items, list):
        return settings_json

    existing_smalltalk = existing_settings.get("smalltalk") if isinstance(existing_settings.get("smalltalk"), dict) else {}
    existing_items = existing_smalltalk.get("items") if isinstance(existing_smalltalk, dict) and isinstance(existing_smalltalk.get("items"), list) else []
    existing_by_id = {
        str(item.get("id") or ""): item
        for item in existing_items
        if isinstance(item, dict)
    }
    actor = current_user.login_id.strip() if isinstance(current_user.login_id, str) and current_user.login_id.strip() else "SYSTEM"
    timestamp = now.isoformat()
    next_items: list[object] = []

    for raw_item in items:
        if not isinstance(raw_item, dict):
            next_items.append(raw_item)
            continue
        item = dict(raw_item)
        existing_item = existing_by_id.get(str(item.get("id") or ""))
        created_by = (
            _settings_meta_string(item, "createdBy", "created_by")
            or _settings_meta_string(existing_item, "createdBy", "created_by")
            or actor
        )
        created_at = (
            _settings_meta_string(item, "createdAt", "created_at")
            or _settings_meta_string(existing_item, "createdAt", "created_at")
            or timestamp
        )
        item["createdBy"] = created_by
        item["createdAt"] = created_at
        item["updatedBy"] = actor
        item["updatedAt"] = timestamp
        next_items.append(item)

    next_smalltalk = dict(smalltalk)
    next_smalltalk["items"] = next_items
    next_settings = dict(settings_json)
    next_settings["smalltalk"] = next_smalltalk
    return next_settings


def _get_version_ai_config(bot: Bot, version: BotVersion | None = None) -> dict[str, object]:
    data_json = bot.data_json if isinstance(bot.data_json, dict) else {}
    config = {key: data_json.get(key) for key in AI_CONFIG_KEYS if key in data_json}
    if version is None:
        return config

    system_config = getattr(version, "system_config_json", None)
    if not isinstance(system_config, dict):
        version_json = normalize_version_document(version.version_json)
        system_config = version_json.get("system_config")
    ai_config = system_config.get("ai_config") if isinstance(system_config, dict) else None
    if isinstance(ai_config, dict):
        config = _merge_json_dict(config, ai_config)
    return config


def _set_version_ai_config(version: BotVersion, updates: dict[str, object]) -> None:
    version_json = normalize_version_document(version.version_json)
    system_config = dict(version_json.get("system_config") or {})
    ai_config = dict(system_config.get("ai_config") or {})
    ai_config = _merge_json_dict(ai_config, updates)
    system_config["ai_config"] = ai_config
    version_json["system_config"] = system_config
    _assign_version_document(version, version_json)


def _is_intent_dialog_document(dialog: dict[str, object]) -> bool:
    value = dialog.get("dialogType")
    if value == 1:
        return True
    return isinstance(value, str) and value.strip() == "1"


def _safe_text_value(value: object) -> str:
    return str(value or "").strip()


def _bot_language(bot: Bot) -> str:
    return _safe_text_value((bot.data_json or {}).get("language")) or "ko"


def _compact_semantic_text(value: object) -> str:
    text = _safe_text_value(value).replace("_", " ").casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


_SEMANTIC_LABEL_MODIFIER_TOKENS = {"요청", "문의", "예정", "확인"}


def _semantic_label_tokens(value: object) -> list[str]:
    text = _safe_text_value(value).replace("_", " ").lower()
    return [token for token in re.split(r"[^0-9a-zA-Z가-힣]+", text) if token]


def _semantic_label_core_tokens(value: object) -> set[str]:
    return {token for token in _semantic_label_tokens(value) if token not in _SEMANTIC_LABEL_MODIFIER_TOKENS}


def _semantic_label_match_score(query: object, label: object) -> float:
    query_compact = _compact_semantic_text(query)
    label_compact = _compact_semantic_text(label)
    if not query_compact or not label_compact:
        return 0.0
    if query_compact == label_compact:
        return 1.0

    query_tokens = set(_semantic_label_tokens(query))
    label_tokens = set(_semantic_label_tokens(label))
    if len(query_tokens) >= 2 and query_tokens.issubset(label_tokens):
        return 0.9

    query_core = _semantic_label_core_tokens(query)
    label_core = _semantic_label_core_tokens(label)
    if len(query_core) >= 2 and query_core == label_core:
        return 0.86

    return 0.0


def _extract_training_utterances(dialog: dict[str, object]) -> list[str]:
    utterances = dialog.get("utterances")
    if not isinstance(utterances, list):
        return []

    texts: list[str] = []
    for item in utterances:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            utterance_type = _safe_text_value(item.get("utteranceType")).upper() or "T"
            if utterance_type != "T":
                continue
            text = _safe_text_value(item.get("text"))
        else:
            text = ""
        if text:
            texts.append(text)
    return texts


def _get_trainable_intent_summary(version_json: dict[str, object]) -> dict[str, object]:
    document = normalize_version_document(version_json)
    intents: list[dict[str, object]] = []
    for dialog in document.get("dialogs", []):
        if not isinstance(dialog, dict) or not _is_intent_dialog_document(dialog):
            continue
        utterances = _extract_training_utterances(dialog)
        if not utterances:
            continue
        intents.append(
            {
                "id": _safe_text_value(dialog.get("id")),
                "name": _safe_text_value(dialog.get("name")) or _safe_text_value(dialog.get("displayName")),
                "utterance_count": len(utterances),
            }
        )

    return {
        "intent_count": len(intents),
        "utterance_count": sum(int(item["utterance_count"]) for item in intents),
        "intents": intents,
    }


def _nlu_engine_training_snapshot(data_json: dict[str, object] | None) -> dict[str, object]:
    data_json = data_json if isinstance(data_json, dict) else {}
    nlu_type = _safe_text_value(data_json.get("nlu_type")) or "ml"
    nlu_model = _safe_text_value(data_json.get("nlu_model") or data_json.get("nlu_engine"))
    if not nlu_model:
        nlu_model = "semantic_engine_default" if nlu_type in SEMANTIC_NLU_TYPES else "deep_learning_lite"
    return {
        "nlu_type": nlu_type,
        "nlu_model": nlu_model,
        "engine_type": nlu_type,
    }


def _build_intent_vector_documents(version_json: dict[str, object]) -> list[dict[str, object]]:
    document = normalize_version_document(version_json)
    items: list[dict[str, object]] = []
    for dialog in document.get("dialogs", []):
        if not isinstance(dialog, dict) or not _is_intent_dialog_document(dialog):
            continue
        utterances = _extract_training_utterances(dialog)
        if not utterances:
            continue
        items.append(
            {
                "intentId": _safe_text_value(dialog.get("id")),
                "intentName": _safe_text_value(dialog.get("name")) or _safe_text_value(dialog.get("displayName")),
                "utterances": utterances,
            }
        )
    return items


def _build_llm_dictionary_terms(version_json: dict[str, object]) -> list[dict[str, object]]:
    document = normalize_version_document(version_json)
    terms: list[dict[str, object]] = []
    for item in document.get("dictionary", []):
        if not isinstance(item, dict):
            continue
        word = _safe_text_value(item.get("word"))
        if not word:
            continue
        synonyms = item.get("synonyms")
        values = [_safe_text_value(value) for value in synonyms] if isinstance(synonyms, list) else []
        terms.append({"name": word, "values": [value for value in values if value]})
    return terms


def _build_llm_entity_terms(version_json: dict[str, object]) -> list[dict[str, object]]:
    document = normalize_version_document(version_json)
    terms: list[dict[str, object]] = []
    for item in document.get("entities", []):
        if not isinstance(item, dict):
            continue
        name = _safe_text_value(item.get("name"))
        if not name:
            continue
        values: list[str] = []
        examples = item.get("examples")
        if isinstance(examples, list):
            values.extend(_safe_text_value(value) for value in examples)
        rows = item.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values.append(_safe_text_value(row.get("value")))
                details = row.get("details")
                if isinstance(details, list):
                    values.extend(_safe_text_value(value) for value in details)
        terms.append({"name": name, "values": [value for value in values if value]})
    return terms


def _is_rag_answer_mode(ai_config: dict[str, object]) -> bool:
    answer_mode = _safe_text_value(ai_config.get("answer_mode"))
    nlu_type = _safe_text_value(ai_config.get("nlu_type")) or "ml"
    return answer_mode in {"semantic_rag", "llm_rag"} and nlu_type in {*SEMANTIC_NLU_TYPES, "llm"}


def _intent_lookup_by_name(version_json: dict[str, object]) -> dict[str, dict[str, str]]:
    document = normalize_version_document(version_json)
    lookup: dict[str, dict[str, str]] = {}
    for dialog in document.get("dialogs", []):
        if not isinstance(dialog, dict) or not _is_intent_dialog_document(dialog):
            continue
        intent_id = _safe_text_value(dialog.get("id"))
        intent_name = _safe_text_value(dialog.get("name")) or _safe_text_value(dialog.get("displayName"))
        if intent_name:
            lookup[_compact_semantic_text(intent_name)] = {"id": intent_id, "name": intent_name}
    return lookup


def _extract_pdf_text_from_bytes(raw: bytes) -> str:
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 답변 파일 내용이 없습니다.")
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="PDF 답변 파일을 처리하려면 pypdf 패키지가 필요합니다.",
            ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 답변 파일을 읽을 수 없습니다.") from exc

    text = "\n\n".join(page.strip() for page in pages if page.strip())
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF에서 추출할 수 있는 텍스트가 없습니다.")
    return text


def _extract_pdf_text(file_base64: str | None) -> str:
    if not file_base64:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 답변 파일 내용이 없습니다.")
    try:
        raw = base64.b64decode(file_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 답변 파일을 읽을 수 없습니다.") from exc
    return _extract_pdf_text_from_bytes(raw)


def _answer_text_chunks(text: str, *, max_chars: int = 1200) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
            continue
        current = f"{current}\n\n{paragraph}".strip() if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _is_likely_rag_heading(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or _is_noise_rag_subject(text):
        return False
    if len(text) > 90:
        return False
    if re.search(r"(?:있습니다|합니다|됩니다|수\s*있습니다|주세요|십시오|입니다)\s*[.。]?$", text):
        return False
    if re.search(r"[.?!。！？]\s*$", text):
        return False
    if re.match(r"^(?:\d+(?:\.\d+)*|[가-힣A-Za-z]\.|[①-⑳])\s+", text):
        return True
    if re.match(r"^[제第]?\s*\d+\s*[장조절항]\s*", text):
        return True
    if ">" in text and len(text) <= 70:
        return True
    compact = _compact_semantic_text(text)
    heading_tokens = (
        "방법",
        "설정",
        "관리",
        "조회",
        "생성",
        "삭제",
        "수정",
        "등록",
        "연동",
        "사용",
        "구성",
        "개요",
        "소개",
        "기능",
        "안내",
        "절차",
        "보장",
        "보험금",
        "면책",
        "계약",
        "약관",
    )
    return 2 <= len(compact) <= 40 and any(token in compact for token in heading_tokens)


def _answer_title_from_text(text: str, fallback: str, index: int) -> str:
    for line in str(text or "").splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        if compact and _is_likely_rag_heading(compact):
            compact = re.sub(r"^[제第]?\s*\d+\s*[장조절항]\s*", "", compact).strip()
            return compact[:60] or f"{fallback} {index}"
    return f"{fallback} {index}"


def _infer_answer_document_title(source_text: str, fallback: str = "") -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(source_text or "").splitlines()]
    candidates: list[tuple[int, str]] = []
    skip_patterns = (
        r"^\d+\s*$",
        r"^page\s*\d+",
        r"^목\s*차$",
        r"^차\s*례$",
        r"^제\s*\d+\s*[조장절항]",
        r"^\[\s*별표",
        r"^별표\s*\d*",
        r"^부\s*칙$",
        r"^보험약관$",
        r"^약관$",
    )
    for index, line in enumerate(lines[:80]):
        if not line or len(line) < 4 or len(line) > 120:
            continue
        lowered = line.lower()
        if lowered.endswith(".pdf"):
            continue
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            continue
        score = 100 - index
        compact = _compact_semantic_text(line)
        if any(token in compact for token in ("보험", "약관", "상품", "계약", "보장", "안내", "설명서", "매뉴얼", "보고서")):
            score += 35
        if re.search(r"[()（）]\s*$", line):
            score -= 8
        if len(line) <= 12:
            score -= 12
        candidates.append((score, line))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if candidates:
        return candidates[0][1]
    fallback_text = _safe_text_value(fallback)
    if fallback_text and not fallback_text.lower().endswith(".pdf"):
        return fallback_text
    return "RAG 답변 문서"


def _parse_answer_training_sections(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_intent = ""
    current_answer: list[str] = []
    in_answer = False

    def flush() -> None:
        nonlocal current_intent, current_answer, in_answer
        answer_text = "\n".join(line for line in current_answer).strip()
        if current_intent and answer_text:
            sections.append({"intent_name": current_intent, "text": answer_text})
        current_answer = []
        in_answer = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        intent_match = re.match(r"^의도\s*[:：]\s*(.+)$", line)
        if intent_match:
            flush()
            current_intent = intent_match.group(1).strip()
            continue
        answer_match = re.match(r"^답변\s*[:：]\s*(.*)$", line)
        if answer_match and current_intent:
            in_answer = True
            rest = answer_match.group(1).strip()
            if rest:
                current_answer.append(rest)
            continue
        if current_intent and (in_answer or line):
            current_answer.append(raw_line.rstrip())
    flush()
    return sections


def _is_noise_rag_subject(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or re.fullmatch(r"[\d\s.,()~-]+", text):
        return True
    if re.fullmatch(r"[ivxlcdm]{1,8}", text, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:목\s*차|차\s*례)\s*>?\s*\d*", text):
        return True
    if re.fullmatch(r"[.\s·…_~-]{4,}\d*", text):
        return True
    if re.search(r"[.\s·…_~-]{8,}\s*\d+\s*$", text):
        return True
    if re.search(r"(?:사용자\s*매뉴얼|user\s*manual)", text, re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:서문|개요|목차|차례)", text):
        return True
    return False


def _build_answer_training_sections_from_chunks(source_text: str, title: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for index, chunk in enumerate(_answer_text_chunks(source_text), start=1):
        section_title = _answer_title_from_text(chunk, title or "PDF 답변", index)
        if _is_noise_rag_subject(section_title):
            continue
        sections.append(
            {
                "intent_name": section_title,
                "text": chunk,
            }
        )
    return sections


def _manual_section_heading_subject(line: str) -> str:
    text = re.sub(r"\s+", " ", str(line or "")).strip()
    if not text or _is_noise_rag_subject(text):
        return ""
    if re.search(r"(?:있습니다|합니다|됩니다|주세요|십시오|입니다)\s*[.。]?$", text):
        return ""
    if re.search(r"[.\s·…_~-]{6,}\s*\d+\s*$", text):
        return ""
    if re.search(r"(?:\s+\d{1,4}){3,}\s*$", text):
        return ""

    looks_numbered = bool(re.match(r"^(?:\d+(?:\.\d+)*|[A-Z]\.|[가-힣]\.|[①-⑳])\s+", text))
    looks_menu_path = ">" in text and len(text) <= 90
    looks_manual_heading = any(
        token in _compact_semantic_text(text)
        for token in (
            "시작하기",
            "생성",
            "관리",
            "등록",
            "수정",
            "삭제",
            "조회",
            "설정",
            "학습",
            "재학습",
            "분석",
            "평가",
            "라이선스",
            "템플릿",
            "대화설계",
            "봇허브",
            "api",
            "qa",
        )
    )
    if not (looks_numbered or looks_menu_path or looks_manual_heading):
        return ""
    return _clean_rag_answer_subject(text)


def _section_body_has_content(lines: list[str]) -> bool:
    body = "\n".join(line.strip() for line in lines if line.strip())
    compact = _compact_semantic_text(body)
    if len(compact) < 20:
        return False
    if re.fullmatch(r"[0-9ivxlcdm\s.·…_~>\-]+", body, re.IGNORECASE):
        return False
    return True


def _build_manual_answer_training_sections(source_text: str, title: str) -> list[dict[str, str]]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(source_text or "").splitlines()]
    sections: list[dict[str, str]] = []
    current_title = ""
    current_lines: list[str] = []
    seen_titles: set[str] = set()

    def flush() -> None:
        nonlocal current_title, current_lines
        title_key = _compact_semantic_text(current_title)
        if current_title and title_key and title_key not in seen_titles and _section_body_has_content(current_lines):
            seen_titles.add(title_key)
            text = "\n".join([current_title, *current_lines]).strip()
            sections.append({"intent_name": current_title, "text": text})
        current_title = ""
        current_lines = []

    for line in lines:
        if not line:
            continue
        subject = _manual_section_heading_subject(line)
        if subject:
            flush()
            current_title = subject
            current_lines = []
            continue
        if current_title and not _is_noise_rag_subject(line):
            current_lines.append(line)
    flush()

    if sections:
        return sections
    return _build_answer_training_sections_from_chunks(source_text, title)


def _detect_answer_document_profile(source_text: str, source_type: str, title: str = "") -> dict[str, object]:
    text = str(source_text or "")
    compact = _compact_semantic_text(f"{title}\n{text}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    clause_hits = len(re.findall(r"(?:^|\n)\s*(?:제\s*\d+\s*[조장절항]|제\d+[조장절항]|\[\s*별표|\b별표\s*\d*)", text))
    table_like_lines = sum(1 for line in lines if line.count("|") >= 2 or line.count("\t") >= 2 or len(re.split(r"\s{2,}", line)) >= 3)
    latin_chars = len(re.findall(r"[A-Za-z]", text))
    korean_chars = len(re.findall(r"[가-힣]", text))
    digit_chars = len(re.findall(r"\d", text))
    total_chars = max(1, len(re.sub(r"\s+", "", text)))
    features = {
        "line_count": len(lines),
        "char_count": total_chars,
        "clause_heading_count": clause_hits,
        "table_like_line_count": table_like_lines,
        "latin_ratio": round(latin_chars / total_chars, 4),
        "korean_ratio": round(korean_chars / total_chars, 4),
        "digit_ratio": round(digit_chars / total_chars, 4),
    }
    manual_tokens = (
        "사용자매뉴얼",
        "사용자안내",
        "사용가이드",
        "매뉴얼",
        "메뉴얼",
        "userguide",
        "usermanual",
        "brityassistant",
        "봇허브",
        "대화이력",
        "라이선스",
        "재학습",
    )
    if source_type == "pdf" and any(token in compact for token in manual_tokens):
        profile = "user_manual"
        chunk_strategy = "manual_section"
    elif source_type == "pdf" and (
        clause_hits >= 3
        or any(token in compact for token in ("약관", "보험계약", "보장내용", "면책", "특약", "별표"))
    ):
        profile = "korean_legal_terms"
        chunk_strategy = "clause"
    elif table_like_lines >= 4:
        profile = "table_or_form"
        chunk_strategy = "section_table"
    elif latin_chars > korean_chars and latin_chars / total_chars >= 0.35:
        profile = "multilingual_or_english"
        chunk_strategy = "semantic_paragraph"
    elif source_type == "pdf" and total_chars >= 20000:
        profile = "long_korean_document"
        chunk_strategy = "semantic_paragraph"
    else:
        profile = "general_korean_document"
        chunk_strategy = "paragraph"
    return {"profile": profile, "chunk_strategy": chunk_strategy, "features": features}


def _select_answer_embedding_engine(document_profile: dict[str, object]) -> dict[str, str]:
    profile = _safe_text_value(document_profile.get("profile"))
    if profile == "korean_legal_terms":
        return {
            "embedding_provider": "ollama",
            "embedding_model": "bge-m3:latest",
            "selection_reason": "조항/약관형 긴 한국어 문서라서 장문 의미 검색에 강한 bge-m3 계열을 사용합니다.",
        }
    if profile == "user_manual":
        return {
            "embedding_provider": "ollama",
            "embedding_model": "bge-m3:latest",
            "selection_reason": "사용자 매뉴얼형 긴 문서라서 장문 의미 검색에 강한 bge-m3 계열을 사용합니다.",
        }
    if profile in {"table_or_form", "multilingual_or_english"}:
        return {
            "embedding_provider": "sentence_transformers",
            "embedding_model": "intfloat/multilingual-e5-large",
            "selection_reason": "표/서식 또는 다국어 문서라서 다국어 검색 성능이 안정적인 multilingual-e5 계열을 사용합니다.",
        }
    if profile == "long_korean_document":
        return {
            "embedding_provider": "ollama",
            "embedding_model": "bge-m3:latest",
            "selection_reason": "긴 한국어 PDF 문서라서 장문 검색에 적합한 bge-m3 계열을 사용합니다.",
        }
    return {
        "embedding_provider": "sentence_transformers",
        "embedding_model": "jhgan/ko-sroberta-multitask",
        "selection_reason": "일반 한국어 문서라서 한국어 문장 임베딩 기본 엔진을 사용합니다.",
    }


def _answer_embedding_engine_from_nlu_config(ai_config: dict[str, object]) -> dict[str, str] | None:
    if _safe_text_value(ai_config.get("answer_mode")) == "llm_rag" and _safe_text_value(ai_config.get("nlu_type")) == "llm":
        llm_provider = _safe_text_value(ai_config.get("llm_provider"))
        llm_model = _safe_text_value(ai_config.get("llm_model"))
        if not llm_provider or not llm_model:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="LLM RAG 답변을 사용하려면 LLM Provider와 모델을 먼저 설정해주세요.",
            )
        return {
            "embedding_provider": f"llm:{llm_provider}",
            "embedding_model": llm_model,
            "selection_reason": "LLM RAG 답변 방식이므로 LLM 엔진 provider/model을 답변 문서 임베딩에도 사용합니다.",
        }
    nlu_type = _safe_text_value(ai_config.get("nlu_type"))
    nlu_model = _safe_text_value(ai_config.get("nlu_model") or ai_config.get("nlu_engine"))
    if nlu_type in {"semantic", "semantic_vector"} and nlu_model in {"", "semantic_engine_default"}:
        return {
            "embedding_provider": "aidot_vector_worker",
            "embedding_model": "semantic_engine_default",
            "selection_reason": "Semantic - Vector Worker 기본 모델을 사용하므로 외부 임베딩 서버에 연결하지 않습니다.",
        }
    if nlu_type != "semantic_external":
        return None
    if nlu_model == "semantic_ollama_bge_m3":
        return {
            "embedding_provider": "ollama",
            "embedding_model": "bge-m3:latest",
            "selection_reason": "Semantic - External Embedding에서 선택한 bge-m3 모델을 사용합니다.",
        }
    if nlu_model == "semantic_embedding_large":
        return {
            "embedding_provider": "sentence_transformers",
            "embedding_model": "intfloat/multilingual-e5-large",
            "selection_reason": "Semantic - External Embedding에서 선택한 multilingual-e5 모델을 사용합니다.",
        }
    if nlu_model == "semantic_embedding_mini":
        return {
            "embedding_provider": "sentence_transformers",
            "embedding_model": "jhgan/ko-sroberta-multitask",
            "selection_reason": "Semantic - External Embedding에서 선택한 ko-sroberta 모델을 사용합니다.",
        }
    return None


def _answer_embedding_engine_for_documents(documents: list[dict[str, object]]) -> dict[str, str]:
    for document in documents:
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        provider = _safe_text_value(metadata.get("embeddingProvider") or metadata.get("embedding_provider"))
        model = _safe_text_value(metadata.get("embeddingModel") or metadata.get("embedding_model"))
        if provider and model:
            return {"embedding_provider": provider, "embedding_model": model}
    return _select_answer_embedding_engine({"profile": "general_korean_document"})


def _answer_training_embedding_options_from_state(version: BotVersion) -> dict[str, str]:
    version_json = normalize_version_document(version.version_json)
    system_config = version_json.get("system_config") if isinstance(version_json.get("system_config"), dict) else {}
    answer_training = system_config.get("answer_training") if isinstance(system_config.get("answer_training"), dict) else {}
    provider = _safe_text_value(answer_training.get("embedding_provider") or answer_training.get("embeddingProvider"))
    model = _safe_text_value(answer_training.get("embedding_model") or answer_training.get("embeddingModel"))
    return {"embedding_provider": provider, "embedding_model": model}


def _has_successful_answer_embedding(version: BotVersion) -> bool:
    version_json = normalize_version_document(version.version_json)
    system_config = version_json.get("system_config") if isinstance(version_json.get("system_config"), dict) else {}
    answer_training = system_config.get("answer_training") if isinstance(system_config.get("answer_training"), dict) else {}
    return (
        _safe_text_value(answer_training.get("status")) == "success"
        and bool(_safe_text_value(answer_training.get("embedding_model") or answer_training.get("embeddingModel")))
    )


def _build_answer_training_documents(
    version_json: dict[str, object],
    payload: VersionNluTrainRequest | None,
    ai_config: dict[str, object],
) -> list[dict[str, object]]:
    if not _is_rag_answer_mode(ai_config):
        return []
    if payload is None or payload.answer_training is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="RAG 답변 방식에서는 학습 전에 답변 텍스트 또는 PDF 파일을 입력해주세요.",
        )

    source = payload.answer_training
    source_text = _safe_text_value(source.text)
    if source.source_type == "pdf" and not source_text:
        source_text = _extract_pdf_text(source.file_base64)
    if not source_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변 학습에 사용할 텍스트가 없습니다.")

    requested_title = _safe_text_value(source.title)
    inferred_title = _infer_answer_document_title(source_text)
    title = requested_title or inferred_title
    file_name = _safe_text_value(source.file_name)
    document_profile = _detect_answer_document_profile(source_text, source.source_type, title)
    selected_embedding_provider = _safe_text_value(source.embedding_provider)
    selected_embedding_model = _safe_text_value(source.embedding_model)
    configured_embedding_engine = _answer_embedding_engine_from_nlu_config(ai_config)
    embedding_engine = (
        {
            "embedding_provider": selected_embedding_provider,
            "embedding_model": selected_embedding_model,
            "selection_reason": "사용자가 답변 문서 임베딩 엔진을 직접 선택했습니다.",
        }
        if selected_embedding_provider and selected_embedding_model
        else configured_embedding_engine or _select_answer_embedding_engine(document_profile)
    )
    intent_lookup = _intent_lookup_by_name(version_json)
    sections = _parse_answer_training_sections(source_text)
    if not sections and source.source_type == "pdf":
        if _safe_text_value(document_profile.get("chunk_strategy")) == "manual_section":
            sections = _build_manual_answer_training_sections(source_text, title)
        else:
            sections = _build_answer_training_sections_from_chunks(source_text, title)
    documents: list[dict[str, object]] = []

    if sections:
        for index, section in enumerate(sections, start=1):
            intent_name = section["intent_name"]
            intent = intent_lookup.get(_compact_semantic_text(intent_name), {})
            text = section["text"]
            document_key = hashlib.sha1(f"{intent_name}:{text}".encode("utf-8")).hexdigest()[:12]
            documents.append(
                {
                    "documentId": f"answer:{document_key}:{index}",
                    "title": intent_name or title,
                    "text": text,
                    "metadata": {
                        "sourceType": source.source_type,
                        "sourceTitle": title,
                        "detectedDocumentTitle": inferred_title,
                        "titleEdited": bool(requested_title and requested_title != inferred_title),
                        "fileName": file_name,
                        "intentId": intent.get("id", ""),
                        "intentName": intent.get("name", intent_name),
                        "documentProfile": document_profile.get("profile"),
                        "chunkStrategy": document_profile.get("chunk_strategy"),
                        "embeddingProvider": embedding_engine["embedding_provider"],
                        "embeddingModel": embedding_engine["embedding_model"],
                        "embeddingSelection": "manual" if selected_embedding_provider and selected_embedding_model else "auto",
                    },
                }
            )
        return documents

    for index, chunk in enumerate(_answer_text_chunks(source_text), start=1):
        document_key = hashlib.sha1(f"{title}:{index}:{chunk}".encode("utf-8")).hexdigest()[:12]
        documents.append(
            {
                "documentId": f"answer:{document_key}:{index}",
                "title": title,
                "text": chunk,
                "metadata": {
                    "sourceType": source.source_type,
                    "sourceTitle": title,
                    "detectedDocumentTitle": inferred_title,
                    "titleEdited": bool(requested_title and requested_title != inferred_title),
                    "fileName": file_name,
                    "intentId": "",
                    "intentName": "",
                    "documentProfile": document_profile.get("profile"),
                    "chunkStrategy": document_profile.get("chunk_strategy"),
                    "embeddingProvider": embedding_engine["embedding_provider"],
                    "embeddingModel": embedding_engine["embedding_model"],
                    "embeddingSelection": "manual" if selected_embedding_provider and selected_embedding_model else "auto",
                },
            }
        )
    if not documents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변 학습에 사용할 문서를 만들 수 없습니다.")
    return documents


def _answer_training_precomputed_value(document: dict[str, object], *, include_pdf: bool = False) -> dict[str, object] | None:
    text = _safe_text_value(document.get("text"))
    if not text:
        return None
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
    if source_type == "pdf" and not include_pdf:
        return None
    intent_id = _safe_text_value(metadata.get("intentId") or metadata.get("intent_id"))
    intent_name = _safe_text_value(metadata.get("intentName") or metadata.get("intent_name"))
    if not intent_id and not intent_name:
        return None
    source_title = _safe_text_value(
        metadata.get("sourceTitle")
        or metadata.get("source_title")
        or metadata.get("fileName")
        or metadata.get("file_name")
        or document.get("title")
    )
    return {
        "documentId": _safe_text_value(document.get("documentId") or document.get("document_id")),
        "title": _safe_text_value(document.get("title")),
        "text": text,
        "score": 1.0,
        "intentId": intent_id,
        "intentName": intent_name,
        "sourceType": source_type,
        "sourceTitle": source_title,
        "page": _safe_text_value(metadata.get("page") or metadata.get("pageNo") or metadata.get("page_no")),
        "metadata": metadata,
    }


def _build_precomputed_answer_training_cache(documents: list[dict[str, object]]) -> dict[str, object]:
    by_intent_id: dict[str, dict[str, object]] = {}
    by_intent_name: dict[str, dict[str, object]] = {}
    for document in documents:
        value = _answer_training_precomputed_value(document)
        if value is None:
            continue
        intent_id = _safe_text_value(value.get("intentId"))
        intent_name = _safe_text_value(value.get("intentName"))
        if intent_id and intent_id not in by_intent_id:
            by_intent_id[intent_id] = value
        intent_name_key = _compact_semantic_text(intent_name)
        if intent_name_key and intent_name_key not in by_intent_name:
            by_intent_name[intent_name_key] = value
    answer_keys = {
        _safe_text_value(value.get("documentId")) or _safe_text_value(value.get("intentId")) or _safe_text_value(value.get("intentName"))
        for value in [*by_intent_id.values(), *by_intent_name.values()]
    }
    return {
        "schema_version": "aidot-rag-answer-precomputed-v1",
        "answer_count": len({key for key in answer_keys if key}),
        "by_intent_id": by_intent_id,
        "by_intent_name": by_intent_name,
    }


def _train_rag_answer_vector_index(
    bot: Bot,
    version: BotVersion,
    ai_config: dict[str, object],
    documents: list[dict[str, object]],
) -> dict[str, object] | None:
    mark_external_version_write(version.id)
    documents = _filter_rag_answer_documents(documents)
    if not documents:
        return None
    config = answer_vector_config(ai_config)
    if not config.is_ready:
        missing = ", ".join(config.missing_fields)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Answer Vector DB 연결 설정이 완료되지 않았습니다. 누락: {missing}",
        )
    try:
        embedding_engine = _answer_embedding_engine_for_documents(documents)
        result = AnswerVectorSearchClient(config).index_answers(
            bot_id=str(bot.id),
            version_id=str(version.id),
            documents=documents,
            embedding_provider=embedding_engine["embedding_provider"],
            embedding_model=embedding_engine["embedding_model"],
        )
    except VectorSearchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    first_metadata = documents[0].get("metadata") if isinstance(documents[0].get("metadata"), dict) else {}

    trained_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "success",
        "trained_at": trained_at,
        "answer_mode": _safe_text_value(ai_config.get("answer_mode")),
        "provider": "external_vector_worker",
        "index_name": config.index_name,
        "endpoint_url": config.endpoint_url,
        "document_profile": _safe_text_value(first_metadata.get("documentProfile")),
        "document_title": _safe_text_value(first_metadata.get("sourceTitle")),
        "detected_document_title": _safe_text_value(first_metadata.get("detectedDocumentTitle")),
        "title_edited": bool(first_metadata.get("titleEdited")),
        "chunk_strategy": _safe_text_value(first_metadata.get("chunkStrategy")),
        "embedding_provider": _safe_text_value(result.get("embeddingProvider")) or embedding_engine["embedding_provider"],
        "embedding_model": _safe_text_value(result.get("embeddingModel")) or embedding_engine["embedding_model"],
        "embedding_selection": _safe_text_value(first_metadata.get("embeddingSelection")) or "auto",
        "document_count": len(documents),
        "indexed": int(result.get("indexed") or len(documents)),
        "precomputed_answers": _build_precomputed_answer_training_cache(documents),
    }


def _is_rag_answer_document_noise(document: dict[str, object]) -> bool:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
    if source_type != "pdf":
        return False
    title = _safe_text_value(document.get("title") or metadata.get("title") or metadata.get("intentName"))
    text = _safe_text_value(document.get("text") or document.get("answer"))
    compact_title = _compact_semantic_text(title)
    compact_text = _compact_semantic_text(text)
    if compact_title.startswith("지원14관리자기능") and "부록2오픈소스고지" in compact_text:
        return True
    if "목차" in title and len(compact_text) < 30:
        return True
    if re.search(r"(?:지원|부록)\s*\d+(?:\.\d+)?\s*", title) and ">" in text and len(compact_text) < 60:
        return True
    return False


def _sanitize_rag_answer_document(document: dict[str, object]) -> dict[str, object]:
    next_document = deepcopy(document)
    text = _safe_text_value(next_document.get("text") or next_document.get("answer"))
    if not text:
        return next_document
    for marker in (
        "\n표기 규약\n",
        "\n서문 > 개정 이력",
        "\n개정 이력\n",
        "\n부록 2. 오픈소스 고지",
        "\n16.부록 2. 오픈소스 고지",
    ):
        index = text.find(marker)
        if index > 0:
            text = text[:index].strip()
    if text:
        next_document["text"] = text
        if "answer" in next_document:
            next_document["answer"] = text
    return next_document


def _filter_rag_answer_documents(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    return [_sanitize_rag_answer_document(document) for document in documents if not _is_rag_answer_document_noise(document)]


def _rag_answer_variable_name(ai_config: dict[str, object]) -> str:
    answer_mode = _safe_text_value(ai_config.get("answer_mode"))
    if answer_mode == "semantic_rag":
        return "$_semantic_answer_text"
    if answer_mode == "llm_rag":
        return "$_llm_answer_text"
    return "$_rag_answer_text"


def _rag_answer_utterances_from_title(title: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", title).strip()
    if not normalized:
        return []
    subject = re.sub(r"\s*문의$", "", normalized).strip()
    candidates = [
        normalized,
        f"{subject} 알려줘",
        f"{subject} 문의",
        f"{subject} 설명해줘",
        f"{subject} 보장 알려줘",
    ]
    seen: set[str] = set()
    utterances: list[str] = []
    for candidate in candidates:
        key = _compact_semantic_text(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        utterances.append(candidate)
    return utterances


def _clean_rag_answer_subject(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or re.fullmatch(r"[\d\s.,()~-]+", text):
        return ""
    if text.startswith("때 "):
        return ""
    if text.endswith(">"):
        return ""
    if (
        "사용자가 메시지를" in text
        or "화면에서 숨기거나 표시합니다" in text
        or text.startswith("이 매뉴얼은 ")
        or text.startswith("Brity Assistant는 ")
        or "시스템을 사용하려면 서버에 접속" in text
        or "회원 가입 승인 요청이 전달" in text
        or "정보를 수정한 후" in text
        or ("개선" in text and "추가" in text and "등" in text)
    ):
        return ""
    if re.search(
        r"(?:참고하세요|클릭하세요|선택하세요|입력하세요|설정하세요|확인하세요|따르세요|클릭해|메시지입니다|표시됩니다|사용합니다|제공합니다|할\s*수\s*있으며|수\s*있습니다|해당\s*문장이\s*정상\s*처리)",
        text,
    ):
        return ""
    if len(_compact_semantic_text(text)) > 35 and re.search(
        r"(?:\b경우\b|사용자가|입력할|설정\s*되었는지|추가하는|대화를\s*설계하여|내용을|메시지를\s*직접|오류\s*사항|의도\s*시작\s*표현|별도의\s*테스트|사용자\s*관리,|개선,.*추가\s*등)",
        text,
    ):
        return ""
    if len(_compact_semantic_text(text)) > 20 and (
        (text.count(",") >= 2 and re.search(r"(?:개선|추가|연계|운영\s*버전|MS\s*Teams|MRQA|Paraphrase)", text, re.IGNORECASE))
        or ("/" in text and "개선" in text)
    ):
        return ""
    if len(_compact_semantic_text(text)) > 25 and (
        "경우" in text
        or "사용자가 원하는" in text
        or "생성되는" in text
        or "TTS 종료후" in text
        or "상담사 전달 기능으로" in text
        or ("개선" in text and "추가" in text and "등" in text)
    ):
        return ""
    if re.search(r"^(?:●|•|○)\s*", text) and re.search(r"(?:참고하세요|클릭하세요|선택하세요|입력하세요|설정하세요)", text):
        return ""
    if re.search(r"^(?:서문|법적\s*고지|펴낸\s*곳|개정\s*이력|사용\s*대상)(?:\s*>|$)", text):
        return ""
    if re.fullmatch(r"(?:있습니다|합니다|됩니다|수\s*있습니다|할\s*수\s*있습니다|사용할\s*수\s*있습니다)", text):
        return ""
    if re.search(r"(?:있습니다|합니다|됩니다|수\s*있습니다)\s*$", text) and len(_compact_semantic_text(text)) < 10:
        return ""
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", text)
    digit_tokens = [token for token in tokens if re.fullmatch(r"\d+", token)]
    if len(digit_tokens) >= 3 and len(digit_tokens) >= max(1, len(tokens) // 2):
        return ""
    if re.fullmatch(r"[ivxlcdm]{1,8}", text, re.IGNORECASE):
        return ""
    if re.fullmatch(r"(?:목\s*차|차\s*례)\s*>?\s*\d*", text):
        return ""
    if re.fullmatch(r"[.\s·…_~-]{4,}\d*", text):
        return ""
    if re.search(r"[.\s·…_~-]{8,}\s*\d+\s*$", text):
        return ""
    if re.search(r"(?:사용자\s*매뉴얼|user\s*manual)", text, re.IGNORECASE):
        return ""
    if re.fullmatch(r"(?:서문|개요|목차|차례)", text):
        return ""
    quoted = [
        item.strip()
        for item in re.findall(r"[\"“”'‘’]([^\"“”'‘’]{2,80})[\"“”'‘’]", text)
        if item.strip()
    ]
    if quoted:
        text = quoted[0]
    text = re.sub(r"^[①-⑳㈜⑴-⑽\s.·,~\-]+", "", text).strip()
    text = re.sub(r"^[제第]?\s*\d+\s*[장조절항]\s*", "", text).strip()
    text = re.sub(r"^\d+(?:\.\d+)*\s*", "", text).strip()
    text = re.sub(r"^(?:\(?주\)?\s*\d+\.?|주\.)\s*", "", text).strip()
    text = re.sub(r"^(?:이\s*)?약관(?:에서|에)?\s*(?:규정하는|정하는)\s*", "", text).strip()
    text = re.sub(r"^(?:다음|아래)(?:과|와)?\s*같은\s*", "", text).strip()
    text = re.sub(r"(?:으로|로)\s*분류되는\s*질병.*$", "", text).strip()
    text = re.sub(r"(?:이라|라고)\s*(?:합니다|한다|함은).*$", "", text).strip()
    text = re.sub(r"(?:을|를|은|는)?\s*제외.*$", "", text).strip()
    text = re.sub(r"^(?:●|•|○)\s*", "", text).strip()
    text = re.sub(r"\s*정의$", "", text).strip()
    text = re.sub(r"(?:\s+\d{1,4}){3,}\s*$", "", text).strip()
    text = re.sub(r"\s+\d{1,4}\s*[-~]\s*\d{1,4}\s*$", "", text).strip()
    text = re.sub(r"(?<=[0-9A-Za-z가-힣)])\s+\d{1,4}\s*$", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip(" .,:;·-")
    if text == "요 기능":
        text = "주요 기능"
    if text.startswith("때 "):
        return ""
    if text.endswith(">"):
        return ""
    if (
        "사용자가 메시지를" in text
        or "화면에서 숨기거나 표시합니다" in text
        or text.startswith("이 매뉴얼은 ")
        or text.startswith("Brity Assistant는 ")
        or "시스템을 사용하려면 서버에 접속" in text
        or "상위 개념으로" in text
        or "즐겨찾기로 설정" in text
        or "회원 가입 승인 요청이 전달" in text
        or "정보를 수정한 후" in text
        or "챗봇이 사용자 질문을 보다 잘 이해" in text
        or "실행할 모듈을 설정" in text
        or "읽기 속도를 설정" in text
        or "학습데이터의 밸런스" in text
        or ("개선" in text and "추가" in text and "등" in text)
    ):
        return ""
    if re.fullmatch(r"(?:있습니다|합니다|됩니다|수\s*있습니다|할\s*수\s*있습니다|사용할\s*수\s*있습니다)", text):
        return ""
    if re.search(r"(?:있습니다|합니다|됩니다|수\s*있습니다)\s*$", text) and len(_compact_semantic_text(text)) < 10:
        return ""
    if re.fullmatch(r"(?:목\s*차|차\s*례)\s*>?\s*\d*", text):
        return ""
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", text)
    digit_tokens = [token for token in tokens if re.fullmatch(r"\d+", token)]
    if len(digit_tokens) >= 3 and len(digit_tokens) >= max(1, len(tokens) // 2):
        return ""
    if re.fullmatch(r"[ivxlcdm]{1,8}", text, re.IGNORECASE):
        return ""
    if re.fullmatch(r"(?:서문|개요|목차|차례)", text):
        return ""
    if re.search(r"^(?:서문|법적\s*고지|펴낸\s*곳|개정\s*이력|사용\s*대상)(?:\s*>|$)", text):
        return ""
    if re.search(r"(?:사용자\s*매뉴얼|user\s*manual)", text, re.IGNORECASE):
        return ""
    if re.search(
        r"(?:참고하세요|클릭하세요|선택하세요|입력하세요|설정하세요|확인하세요|따르세요|클릭해|메시지입니다|표시됩니다|사용합니다|제공합니다|설정합니다|기능입니다|할\s*수\s*있으며|수\s*있습니다|해당\s*문장이\s*정상\s*처리)",
        text,
    ):
        return ""
    if len(_compact_semantic_text(text)) > 35 and re.search(
        r"(?:\b경우\b|사용자가|입력할|설정\s*되었는지|추가하는|대화를\s*설계하여|내용을|메시지를\s*직접|오류\s*사항|의도\s*시작\s*표현|별도의\s*테스트|사용자\s*관리,|개선,.*추가\s*등)",
        text,
    ):
        return ""
    if len(_compact_semantic_text(text)) > 20 and (
        (text.count(",") >= 2 and re.search(r"(?:개선|추가|연계|운영\s*버전|MS\s*Teams|MRQA|Paraphrase)", text, re.IGNORECASE))
        or ("/" in text and "개선" in text)
    ):
        return ""
    if len(_compact_semantic_text(text)) > 25 and (
        "경우" in text
        or "사용자가 원하는" in text
        or "생성되는" in text
        or "TTS 종료후" in text
        or "상담사 전달 기능으로" in text
        or ("개선" in text and "추가" in text and "등" in text)
    ):
        return ""
    if text.endswith("하기") and len(text) > 2:
        text = f"{text[:-2]} 방법"
    elif text.endswith("하기 안내") and len(text) > 5:
        text = f"{text[:-5]} 방법"
    if len(_compact_semantic_text(text)) < 2:
        return ""
    return text[:60]


def _rag_answer_intent_name_from_document(document: dict[str, object], index: int) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
    explicit_intent = _safe_text_value(metadata.get("intentName") or metadata.get("intent_name"))
    if source_type != "pdf" and explicit_intent:
        return explicit_intent[:120]

    if source_type == "pdf":
        candidates = [
            _safe_text_value(document.get("title")),
            _safe_text_value(metadata.get("intentName") or metadata.get("intent_name")),
        ]
    else:
        candidates = [
            _safe_text_value(document.get("title")),
            _safe_text_value(metadata.get("intentName") or metadata.get("intent_name")),
            *_safe_text_value(document.get("text")).splitlines()[:6],
        ]
    for candidate in candidates:
        subject = _clean_rag_answer_subject(candidate)
        if subject:
            return f"{subject} 문의"

    source_title = _clean_rag_answer_subject(_safe_text_value(metadata.get("sourceTitle") or metadata.get("source_title")))
    if source_title:
        return f"{source_title} 내용 문의"
    return f"문서 내용 문의 {index}"


def _rag_answer_parent_intent_name_from_document(document: dict[str, object]) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
    if source_type != "pdf":
        return ""
    raw_title = _safe_text_value(document.get("title") or metadata.get("title") or metadata.get("intentName"))
    if ">" not in raw_title:
        return ""
    parent = _clean_rag_answer_subject(raw_title.split(">", 1)[0])
    if not parent or _compact_semantic_text(parent) in {"시작", "개요"}:
        return ""
    return f"{parent} 문의"


def _rag_answer_group_priority(name: str) -> int:
    subject = name[:-3].strip() if name.endswith(" 문의") else name.strip()
    compact = _compact_semantic_text(subject)
    if not compact:
        return 90
    if subject.endswith("사용 방법"):
        return 0
    if ">" in subject:
        return 4
    if re.search(r"(?:,|/|●|부록|개요)", subject):
        return 7
    if len(compact) > 32:
        return 8
    if re.search(r"(?:방법|관리|조회|등록|생성|삭제|설정|학습|분석|기능|라이선스)", subject):
        return 1
    return 3


def _build_rag_answer_configure_groups(
    documents: list[dict[str, object]],
    *,
    target_count: int,
    target_count_policy: str,
    ai_config: dict[str, object],
) -> list[dict[str, object]]:
    documents = _filter_rag_answer_documents(documents)
    if not documents:
        return []
    limited_target = max(1, min(int(target_count or 1), 100))
    if target_count_policy == "minimize":
        group_limit = min(limited_target, len(documents))
    elif target_count_policy == "exact":
        group_limit = min(limited_target, len(documents))
    else:
        group_limit = min(max(1, limited_target), len(documents))

    candidates: list[tuple[int, int, str, dict[str, object]]] = []
    used_names: set[str] = set()
    answer_variable = _rag_answer_variable_name(ai_config)
    for document_index, document in enumerate(documents, start=1):
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
        names = [_rag_answer_parent_intent_name_from_document(document), _rag_answer_intent_name_from_document(document, document_index)]
        for name in names:
            name_key = _compact_semantic_text(name)
            if not name_key:
                continue
            if source_type == "pdf" and name.startswith("문서 내용 문의"):
                continue
            if source_type == "pdf" and name_key in used_names:
                continue
            used_names.add(name_key)
            candidates.append((_rag_answer_group_priority(name), document_index, name, document))

    groups: list[dict[str, object]] = []
    for _, _, name, document in sorted(candidates, key=lambda item: (item[0], item[1])):
        if len(groups) >= group_limit:
            break
        utterances = _rag_answer_utterances_from_title(name)
        if not utterances:
            utterances = [f"문서 내용 문의 {len(groups) + 1}"]
        groups.append(
            {
                "id": f"rag-answer-{len(groups) + 1}",
                "name": name,
                "answer": answer_variable,
                "utterances": utterances,
                "reason": "답변 문서 임베딩 결과에서 생성한 RAG 의도 후보입니다.",
                "documentId": _safe_text_value(document.get("documentId") or document.get("document_id")),
            }
        )
    return groups


def _rag_answer_candidate_utterances_from_documents(documents: list[dict[str, object]], *, limit: int = 300) -> list[str]:
    documents = _filter_rag_answer_documents(documents)
    utterances: list[str] = []
    seen: set[str] = set()
    seen_subjects: set[str] = set()
    for document_index, document in enumerate(documents, start=1):
        if len(utterances) >= limit:
            break
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        source_type = _safe_text_value(metadata.get("sourceType") or metadata.get("source_type"))
        if source_type == "pdf":
            candidates = [
                _safe_text_value(document.get("title")),
                _safe_text_value(metadata.get("intentName") or metadata.get("intent_name")),
            ]
        else:
            candidates = [
                _safe_text_value(document.get("title")),
                _safe_text_value(metadata.get("intentName") or metadata.get("intent_name")),
                *_safe_text_value(document.get("text")).splitlines()[:8],
            ]
        for candidate in candidates:
            subject = _clean_rag_answer_subject(candidate)
            if not subject:
                continue
            subject_key = _compact_semantic_text(subject)
            if not subject_key or subject_key in seen_subjects:
                continue
            seen_subjects.add(subject_key)
            utterance = f"{subject} 문의"
            key = _compact_semantic_text(utterance)
            if not key or key in seen:
                continue
            seen.add(key)
            utterances.append(utterance)
            if len(utterances) >= limit:
                break
        if len(utterances) < limit and not candidates:
            utterance = f"문서 내용 문의 {document_index}"
            key = _compact_semantic_text(utterance)
            if key and key not in seen:
                seen.add(key)
                utterances.append(utterance)
    return utterances


def _build_llm_rag_answer_configure_groups(
    documents: list[dict[str, object]],
    *,
    target_count: int,
    target_count_policy: str,
    ai_config: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    utterances = _rag_answer_candidate_utterances_from_documents(documents)
    if not utterances:
        return _build_rag_answer_configure_groups(
            documents,
            target_count=target_count,
            target_count_policy=target_count_policy,
            ai_config=ai_config,
        ), {"intent_generation": "fallback", "fallback_reason": "LLM 의도 구성에 사용할 후보 문장을 만들 수 없습니다."}
    try:
        result = configure_intents_with_llm(
            provider=_safe_text_value(ai_config.get("llm_provider")),
            model=_safe_text_value(ai_config.get("llm_model")),
            api_key=None,
            base_url=_safe_text_value(ai_config.get("llm_base_url")),
            timeout_seconds=None,
            utterances=utterances[:500],
            target_count=target_count,
            target_count_policy=target_count_policy,
            dictionary_terms=[],
            entity_terms=[],
        )
    except LlmClientError as exc:
        return _build_rag_answer_configure_groups(
            documents,
            target_count=target_count,
            target_count_policy=target_count_policy,
            ai_config=ai_config,
        ), {
            "intent_generation": "fallback",
            "fallback_reason": str(exc),
            "llm_provider": _safe_text_value(ai_config.get("llm_provider")),
            "llm_model": _safe_text_value(ai_config.get("llm_model")),
        }

    answer_variable = _rag_answer_variable_name(ai_config)
    groups = [
        {
            "id": f"llm-rag-answer-{index + 1}",
            "name": group.name,
            "answer": answer_variable,
            "utterances": group.utterances,
            "reason": group.reason or "LLM이 RAG 답변 문서 후보 문장을 묶어 생성한 의도 후보입니다.",
        }
        for index, group in enumerate(result.groups)
        if group.name and group.utterances
    ]
    if not groups:
        return _build_rag_answer_configure_groups(
            documents,
            target_count=target_count,
            target_count_policy=target_count_policy,
            ai_config=ai_config,
        ), {"intent_generation": "fallback", "fallback_reason": "LLM 의도 구성 결과가 비어 있습니다."}
    return groups, {
        "intent_generation": "llm",
        "llm_provider": result.provider,
        "llm_model": result.model,
        "latency_ms": result.latency_ms,
        "candidate_utterance_count": len(utterances),
    }


def _configure_rag_answer_documents(
    *,
    db: Session,
    request: Request,
    current_user: User,
    bot: Bot,
    version: BotVersion,
    ai_config: dict[str, object],
    documents: list[dict[str, object]],
    target_count: int,
    target_count_policy: str,
) -> dict[str, object]:
    answer_training_result = _train_rag_answer_vector_index(bot, version, ai_config, documents)
    if answer_training_result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변 임베딩에 사용할 문서가 없습니다.")
    _save_answer_training_result(
        db=db,
        request=request,
        current_user=current_user,
        bot=bot,
        version=version,
        answer_training_result=answer_training_result,
    )
    if _safe_text_value(ai_config.get("nlu_type")) == "llm" and _safe_text_value(ai_config.get("answer_mode")) == "llm_rag":
        groups, diagnostics = _build_llm_rag_answer_configure_groups(
            documents,
            target_count=target_count,
            target_count_policy=target_count_policy,
            ai_config=ai_config,
        )
    else:
        groups = _build_rag_answer_configure_groups(
            documents,
            target_count=target_count,
            target_count_policy=target_count_policy,
            ai_config=ai_config,
        )
        diagnostics = {"intent_generation": "rule"}
    return {
        "provider": answer_training_result.get("provider") or "external_vector_worker",
        "model": answer_training_result.get("embedding_model") or "",
        "latency_ms": 0,
        "target_count": target_count,
        "target_count_policy": target_count_policy,
        "groups": groups,
        "answer_training": answer_training_result,
        "diagnostics": diagnostics,
    }


def _save_answer_training_result(
    *,
    db: Session,
    request: Request,
    current_user: User,
    bot: Bot,
    version: BotVersion,
    answer_training_result: dict[str, object],
) -> None:
    version_json = normalize_version_document(version.version_json)
    system_config = dict(version_json.get("system_config") or {})
    system_config["answer_training"] = answer_training_result
    version_json["system_config"] = system_config
    _assign_version_document(version, version_json)
    now = datetime.now(timezone.utc)
    version.updated_at = now
    bot.updated_at = now
    db.add(bot)
    db.add(version)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.version.answer_rag.embed",
        target_type="bot_version",
        target_id=version.id,
        after_json=answer_training_result,
    )
    db.commit()
    _purge_version_cache(version)


def _export_answer_vector_index(bot: Bot, version: BotVersion, ai_config: dict[str, object]) -> dict[str, object] | None:
    if not _is_rag_answer_mode(ai_config):
        return None
    config = answer_vector_config(ai_config)
    if not config.is_ready:
        return None
    try:
        exported = AnswerVectorSearchClient(config).export_answers(
            bot_id=str(bot.id),
            version_id=str(version.id),
        )
    except VectorSearchError:
        return None
    return exported


def _import_answer_vector_index(bot: Bot, version: BotVersion, ai_config: dict[str, object], payload: object) -> dict[str, object] | None:
    mark_external_version_write(version.id)
    if not isinstance(payload, dict) or not _is_rag_answer_mode(ai_config):
        return None
    config = answer_vector_config(ai_config)
    if not config.is_ready:
        return None
    try:
        result = AnswerVectorSearchClient(config).import_answers(
            bot_id=str(bot.id),
            version_id=str(version.id),
            payload=payload,
        )
    except VectorSearchError:
        return None
    if not isinstance(result, dict):
        return None
    try:
        imported = int(result.get("imported") or 0)
    except (TypeError, ValueError):
        imported = 0
    return result if imported > 0 else None


def _copy_answer_vector_index(bot: Bot, source_version: BotVersion, target_version: BotVersion, ai_config: dict[str, object]) -> dict[str, object] | None:
    mark_external_version_write(target_version.id)
    if not _is_rag_answer_mode(ai_config):
        return None
    config = answer_vector_config(ai_config)
    if not config.is_ready:
        return None
    try:
        result = AnswerVectorSearchClient(config).copy_answers(
            source_bot_id=str(bot.id),
            source_version_id=str(source_version.id),
            target_bot_id=str(bot.id),
            target_version_id=str(target_version.id),
        )
    except VectorSearchError:
        return None
    return result if isinstance(result, dict) else None


def _mark_answer_training_reembed_required(version_json: dict[str, object], reason: str) -> dict[str, object]:
    document = normalize_version_document(version_json)
    system_config = dict(document.get("system_config") or {})
    answer_training = system_config.get("answer_training")
    if isinstance(answer_training, dict) and _safe_text_value(answer_training.get("status")) == "success":
        next_answer_training = dict(answer_training)
        next_answer_training["status"] = "reembed_required"
        next_answer_training["reembed_required_reason"] = reason
        next_answer_training["reembed_required_at"] = datetime.now(timezone.utc).isoformat()
        system_config["answer_training"] = next_answer_training
        document["system_config"] = system_config
    return document


def _mark_nlu_training_retrain_required(version_json: dict[str, object], reason: str) -> dict[str, object]:
    document = normalize_version_document(version_json)
    system_config = dict(document.get("system_config") or {})
    training = system_config.get("nlu_training")
    if isinstance(training, dict) and _safe_text_value(training.get("status")) == "success":
        next_training = dict(training)
        next_training["status"] = "retrain_required"
        next_training["retrain_required_reason"] = reason
        next_training["retrain_required_at"] = datetime.now(timezone.utc).isoformat()
        system_config["nlu_training"] = next_training
        document["system_config"] = system_config
    return document


def _answer_vector_config_fingerprint(ai_config: dict[str, object]) -> tuple[bool, str, str, str]:
    config = answer_vector_config(ai_config)
    return (config.enabled, config.endpoint_url.strip(), config.index_name.strip(), config.api_key.strip())


def _semantic_eval_rows_from_documents(vector_documents: list[dict[str, object]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in vector_documents:
        intent_id = _safe_text_value(item.get("intentId"))
        intent_name = _safe_text_value(item.get("intentName"))
        utterances = item.get("utterances")
        if not isinstance(utterances, list):
            continue
        for utterance in utterances:
            text = _safe_text_value(utterance)
            if text:
                rows.append({"dialog_id": intent_id, "dialog_name": intent_name, "text": text})
    return rows


def _semantic_documents_from_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        dialog_id = row["dialog_id"]
        if dialog_id not in grouped:
            grouped[dialog_id] = {
                "intentId": dialog_id,
                "intentName": row["dialog_name"],
                "utterances": [],
            }
        utterances = grouped[dialog_id]["utterances"]
        if isinstance(utterances, list):
            utterances.append(row["text"])
    return list(grouped.values())


def _semantic_exact_intent_lookup(vector_documents: list[dict[str, object]]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for key, matches in _semantic_exact_training_lookup(vector_documents).items():
        unique_by_dialog: dict[str, dict[str, str]] = {}
        for match in matches:
            dialog_id = match["dialog_id"]
            if dialog_id not in unique_by_dialog:
                unique_by_dialog[dialog_id] = match
        if len(unique_by_dialog) == 1:
            lookup[key] = next(iter(unique_by_dialog.values()))
    return lookup


def _semantic_label_intent_match(vector_documents: list[dict[str, object]], utterance: object) -> dict[str, object] | None:
    candidates: list[dict[str, object]] = []
    seen_intents: set[str] = set()
    for item in vector_documents:
        intent_id = _safe_text_value(item.get("intentId"))
        intent_name = _safe_text_value(item.get("intentName"))
        if not intent_id or not intent_name or intent_id in seen_intents:
            continue
        score = _semantic_label_match_score(utterance, intent_name)
        if score <= 0:
            continue
        candidates.append(
            {
                "dialog_id": intent_id,
                "dialog_name": intent_name,
                "score": score,
            }
        )
        seen_intents.add(intent_id)

    candidates.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    top_score = float(candidates[0].get("score") or 0.0)
    next_score = float(candidates[1].get("score") or 0.0)
    if top_score >= 0.9 or top_score > next_score:
        return candidates[0]
    return None


def _semantic_exact_training_lookup(vector_documents: list[dict[str, object]]) -> dict[str, list[dict[str, str]]]:
    lookup: dict[str, list[dict[str, str]]] = {}
    for item in vector_documents:
        intent_id = _safe_text_value(item.get("intentId"))
        intent_name = _safe_text_value(item.get("intentName"))
        if not intent_id or not intent_name:
            continue
        candidates: list[tuple[str, str]] = [(intent_name, "intentName exact")]
        utterances = item.get("utterances")
        if isinstance(utterances, list):
            candidates.extend((_safe_text_value(utterance), "utterance exact") for utterance in utterances)
        for text, source in candidates:
            key = _compact_semantic_text(text)
            if not key:
                continue
            lookup.setdefault(key, []).append(
                {
                    "dialog_id": intent_id,
                    "dialog_name": intent_name,
                    "source": source,
                    "matched_text": text,
                }
            )
    return lookup


def _split_semantic_eval_rows(rows: list[dict[str, str]], *, seed: str, randomize: bool) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rng = random.Random(seed)
    rows_by_dialog: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_dialog.setdefault(row["dialog_id"], []).append(row)

    train_rows: list[dict[str, str]] = []
    test_rows: list[dict[str, str]] = []
    for dialog_rows in rows_by_dialog.values():
        split_rows = dialog_rows[:]
        if randomize:
            rng.shuffle(split_rows)
        if len(split_rows) <= 1:
            train_rows.extend(split_rows)
            continue
        test_count = max(1, round(len(split_rows) * 0.1))
        test_rows.extend(split_rows[:test_count])
        train_rows.extend(split_rows[test_count:])
    return train_rows, test_rows


def _semantic_match_to_row(row: dict[str, str], matches: list[object], *, row_id: str) -> dict[str, object]:
    scores: list[dict[str, object]] = []
    for match in matches:
        intent_id = _safe_text_value(getattr(match, "intent_id", ""))
        intent_name = _safe_text_value(getattr(match, "intent_name", ""))
        score = float(getattr(match, "score", 0.0) or 0.0)
        scores.append(
            {
                "dialog_id": intent_id,
                "dialog_name": intent_name,
                "score": round(score * 100, 4),
                "features": [_safe_text_value(getattr(match, "matched_text", ""))],
            }
        )
    label_score = _semantic_label_match_score(row["text"], row["dialog_name"])
    if label_score > 0:
        expected_score = round(label_score * 100, 4)
        matched_expected = False
        for score_row in scores:
            if _safe_text_value(score_row.get("dialog_id")) != row["dialog_id"]:
                continue
            if float(score_row.get("score") or 0.0) < expected_score:
                score_row["score"] = expected_score
                score_row["features"] = [row["dialog_name"]]
            matched_expected = True
            break
        if not matched_expected:
            scores.append(
                {
                    "dialog_id": row["dialog_id"],
                    "dialog_name": row["dialog_name"],
                    "score": expected_score,
                    "features": [row["dialog_name"]],
                }
            )
        scores.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    top = scores[0] if scores else {}
    predicted_dialog_id = _safe_text_value(top.get("dialog_id"))
    predicted_name = _safe_text_value(top.get("dialog_name")) or "-"
    score = float(top.get("score") or 0.0)
    top_features = top.get("features")
    matched_text = _safe_text_value(top_features[0]) if isinstance(top_features, list) and top_features else ""
    return {
        "id": row_id,
        "utterance": row["text"],
        "expected_dialog_id": row["dialog_id"],
        "expected_name": row["dialog_name"],
        "predicted_dialog_id": predicted_dialog_id,
        "predicted_name": predicted_name,
        "score": score,
        "features": [matched_text] if matched_text else [],
        "correct": predicted_dialog_id == row["dialog_id"],
        "scores": scores,
    }


def _semantic_accuracy(rows: list[dict[str, object]]) -> float | None:
    if not rows:
        return None
    return sum(1 for row in rows if row.get("correct") is True) / len(rows)


def _llm_training_snapshot_for_rows(
    training_snapshot: dict[str, object],
    train_rows: list[dict[str, str]],
) -> dict[str, object]:
    snapshot = deepcopy(training_snapshot)
    snapshot_payload = snapshot.get("snapshot")
    snapshot_payload = dict(snapshot_payload) if isinstance(snapshot_payload, dict) else {}
    intents = _semantic_documents_from_rows(train_rows)
    snapshot_payload["intents"] = intents
    snapshot["snapshot"] = snapshot_payload
    snapshot["intent_count"] = len(intents)
    snapshot["utterance_count"] = sum(len(item.get("utterances", [])) for item in intents)
    counts = snapshot.get("counts")
    if isinstance(counts, dict):
        snapshot["counts"] = {**counts, "intent_documents": snapshot["utterance_count"]}
    return snapshot


def _llm_classification_to_row(row: dict[str, str], result, *, row_id: str) -> dict[str, object]:
    scores: list[dict[str, object]] = []
    for candidate in getattr(result, "candidates", []):
        score = float(getattr(candidate, "confidence", 0.0) or 0.0)
        reason = _safe_text_value(getattr(candidate, "reason", ""))
        scores.append(
            {
                "dialog_id": _safe_text_value(getattr(candidate, "intent_id", "")),
                "dialog_name": _safe_text_value(getattr(candidate, "intent_name", "")),
                "score": round(score * 100, 4),
                "features": [reason] if reason else [],
            }
        )
    top = scores[0] if scores else {}
    predicted_dialog_id = _safe_text_value(top.get("dialog_id"))
    predicted_name = _safe_text_value(top.get("dialog_name")) or "-"
    features = top.get("features")
    return {
        "id": row_id,
        "utterance": row["text"],
        "expected_dialog_id": row["dialog_id"],
        "expected_name": row["dialog_name"],
        "predicted_dialog_id": predicted_dialog_id,
        "predicted_name": predicted_name,
        "score": float(top.get("score") or 0.0),
        "features": features if isinstance(features, list) else [],
        "correct": predicted_dialog_id == row["dialog_id"],
        "scores": scores,
    }


def _llm_error_to_row(row: dict[str, str], error: Exception, *, row_id: str) -> dict[str, object]:
    return {
        "id": row_id,
        "utterance": row["text"],
        "expected_dialog_id": row["dialog_id"],
        "expected_name": row["dialog_name"],
        "predicted_dialog_id": "",
        "predicted_name": "-",
        "score": 0.0,
        "features": [str(error)[:300]],
        "correct": False,
        "scores": [],
        "error": str(error)[:1000],
    }


def _evaluate_llm_split(
    *,
    training_snapshot: dict[str, object],
    ai_config: dict[str, object],
    train_rows: list[dict[str, str]],
    test_rows: list[dict[str, str]],
    top_k: int,
    index_suffix: str,
) -> tuple[float | None, list[dict[str, object]], dict[str, object]]:
    if not test_rows:
        return None, [], {"evaluated_count": 0, "failure_count": 0, "latency_ms_total": 0}
    eval_snapshot = _llm_training_snapshot_for_rows(training_snapshot, train_rows)
    evaluated_rows: list[dict[str, object]] = []
    latency_ms_total = 0
    failure_count = 0
    for index, row in enumerate(test_rows):
        try:
            result = classify_intent_with_llm_snapshot(
                training_snapshot=eval_snapshot,
                ai_config=ai_config,
                query=row["text"],
                top_k=top_k,
            )
            latency_ms_total += int(getattr(result, "latency_ms", 0) or 0)
            evaluated_rows.append(_llm_classification_to_row(row, result, row_id=f"{index_suffix}-{index}"))
        except Exception as exc:  # LLM 평가는 학습 자체를 막지 않고 실패 케이스로 남긴다.
            failure_count += 1
            evaluated_rows.append(_llm_error_to_row(row, exc, row_id=f"{index_suffix}-{index}"))
    return (
        _semantic_accuracy(evaluated_rows),
        evaluated_rows,
        {
            "evaluated_count": len(test_rows),
            "failure_count": failure_count,
            "latency_ms_total": latency_ms_total,
        },
    )


def _calculate_llm_nlu_evaluation(
    *,
    version: BotVersion,
    ai_config: dict[str, object],
    training_snapshot: dict[str, object],
    trained_at: str,
    top_k: int,
) -> dict[str, object]:
    snapshot_payload = training_snapshot.get("snapshot")
    snapshot_payload = snapshot_payload if isinstance(snapshot_payload, dict) else {}
    intents = snapshot_payload.get("intents")
    vector_documents = intents if isinstance(intents, list) else []
    all_rows = _semantic_eval_rows_from_documents(vector_documents)
    fixed_train_rows, fixed_test_rows = _split_semantic_eval_rows(
        all_rows,
        seed=f"{version.id}:{trained_at}:fixed:llm",
        randomize=False,
    )
    random_train_rows, random_test_rows = _split_semantic_eval_rows(
        all_rows,
        seed=f"{version.id}:{trained_at}:random:llm",
        randomize=True,
    )
    fixed_accuracy, fixed_rows, fixed_stats = _evaluate_llm_split(
        training_snapshot=training_snapshot,
        ai_config=ai_config,
        train_rows=fixed_train_rows,
        test_rows=fixed_test_rows,
        top_k=top_k,
        index_suffix="llm-fixed-eval",
    )
    random_accuracy, random_rows, random_stats = _evaluate_llm_split(
        training_snapshot=training_snapshot,
        ai_config=ai_config,
        train_rows=random_train_rows,
        test_rows=random_test_rows,
        top_k=top_k,
        index_suffix="llm-random-eval",
    )
    rows = [*fixed_rows, *random_rows]
    failure_count = int(fixed_stats.get("failure_count") or 0) + int(random_stats.get("failure_count") or 0)
    evaluated_count = int(fixed_stats.get("evaluated_count") or 0) + int(random_stats.get("evaluated_count") or 0)
    latency_ms_total = int(fixed_stats.get("latency_ms_total") or 0) + int(random_stats.get("latency_ms_total") or 0)
    gap = abs(random_accuracy - fixed_accuracy) if random_accuracy is not None and fixed_accuracy is not None else None
    status_text = "success"
    if evaluated_count and failure_count:
        status_text = "failed" if failure_count >= evaluated_count else "partial"
    elif not evaluated_count:
        status_text = "skipped"
    return {
        "trained_at": trained_at,
        "engine_type": "llm",
        "nlu_type": "llm",
        "nlu_model": training_snapshot.get("nlu_model"),
        "provider": training_snapshot.get("provider"),
        "model": training_snapshot.get("model"),
        "random_accuracy": random_accuracy,
        "fixed_accuracy": fixed_accuracy,
        "gap": gap,
        "intent_count": len({row["dialog_id"] for row in all_rows if row["dialog_id"]}),
        "training_utterance_count": len(all_rows),
        "validation_utterance_count": len(fixed_test_rows),
        "random_train_count": len(random_train_rows),
        "random_test_count": len(random_test_rows),
        "evaluation_status": status_text,
        "evaluated_count": evaluated_count,
        "failure_count": failure_count,
        "average_latency_ms": round(latency_ms_total / max(1, evaluated_count - failure_count)) if evaluated_count != failure_count else None,
        "message": "LLM NLU 학습 스냅샷 기준으로 9:1 Split 평가를 수행했습니다.",
        "snapshot": {"training_rows": rows},
        "quality_diagnostics": {
            "summary": {
                "engine_type": "llm",
                "evaluated_count": evaluated_count,
                "failure_count": failure_count,
                "fixed_test_count": len(fixed_rows),
                "random_test_count": len(random_rows),
                "average_latency_ms": round(latency_ms_total / max(1, evaluated_count - failure_count))
                if evaluated_count != failure_count
                else None,
            },
            "items": [row for row in rows if row.get("correct") is not True],
        },
    }


def _semantic_exact_match_to_row(row: dict[str, str], match: dict[str, str], *, row_id: str) -> dict[str, object]:
    predicted_dialog_id = match["dialog_id"]
    predicted_name = match["dialog_name"]
    source = _safe_text_value(match.get("source")) or "intentName exact"
    matched_text = _safe_text_value(match.get("matched_text")) or source
    return {
        "id": row_id,
        "utterance": row["text"],
        "expected_dialog_id": row["dialog_id"],
        "expected_name": row["dialog_name"],
        "predicted_dialog_id": predicted_dialog_id,
        "predicted_name": predicted_name,
        "score": 100.0,
        "features": [matched_text],
        "correct": predicted_dialog_id == row["dialog_id"],
        "scores": [
            {
                "dialog_id": predicted_dialog_id,
                "dialog_name": predicted_name,
                "score": 100.0,
                "features": [source],
            }
        ],
    }


def _semantic_quality_item_from_row(row: dict[str, object], *, row_type: str = "T") -> dict[str, object]:
    scores = row.get("scores") if isinstance(row.get("scores"), list) else []
    score_rows = [score for score in scores if isinstance(score, dict)]
    top = score_rows[0] if score_rows else {}
    second = score_rows[1] if len(score_rows) > 1 else {}
    expected_id = _safe_text_value(row.get("expected_dialog_id"))
    expected_score = 0.0
    for score in score_rows:
        if _safe_text_value(score.get("dialog_id")) == expected_id:
            expected_score = float(score.get("score") or 0.0)
            break
    features = row.get("features")
    if not isinstance(features, list):
        features = []
    predicted_name = _safe_text_value(row.get("predicted_name")) or "-"
    diagnosis_type = "Semantic 검색 불일치"
    status_text = "오분류"
    reason = "전체 학습 인덱스에서 기대 의도와 다른 의도가 1순위로 검색되었습니다."
    if _safe_text_value(row.get("diagnosis_type")) == "중복 학습문장":
        diagnosis_type = "중복 학습문장"
        status_text = "중복"
        reason = "동일한 학습문장이 여러 의도에 등록되어 정확한 의도를 하나로 결정할 수 없습니다."
    return {
        **row,
        "row_type": row_type,
        "status": status_text,
        "diagnosis_type": diagnosis_type,
        "expected_score": round(expected_score, 4),
        "top_score": round(float(top.get("score") or row.get("score") or 0.0), 4),
        "second_name": _safe_text_value(second.get("dialog_name")) or "-",
        "second_score": round(float(second.get("score") or 0.0), 4),
        "features": [_safe_text_value(feature) for feature in features if _safe_text_value(feature)],
        "reason": reason,
        "recommendation": "구성 결과와 학습 인덱스가 다릅니다. 동일 문장 중복, 사전 정규화, Vector DB 인덱싱 결과를 확인해주세요.",
        "predicted_name": predicted_name,
    }


def _build_semantic_training_quality_diagnostics(
    *,
    bot: Bot,
    version: BotVersion,
    config,
    vector_documents: list[dict[str, object]],
    top_k: int,
) -> dict[str, object]:
    rows = _semantic_eval_rows_from_documents(vector_documents)
    exact_lookup = _semantic_exact_training_lookup(vector_documents)
    client = IntentVectorSearchClient(config)
    dictionary_terms = _build_llm_dictionary_terms(version.version_json)
    items: list[dict[str, object]] = []
    exact_match_count = 0
    for index, row in enumerate(rows):
        exact_matches = exact_lookup.get(_compact_semantic_text(row["text"]), [])
        unique_by_dialog: dict[str, dict[str, str]] = {}
        for match in exact_matches:
            dialog_id = match["dialog_id"]
            if dialog_id not in unique_by_dialog:
                unique_by_dialog[dialog_id] = match
        exact_matches = list(unique_by_dialog.values())
        if len(exact_matches) == 1 and exact_matches[0]["dialog_id"] == row["dialog_id"]:
            exact_match_count += 1
            continue
        if len(exact_matches) > 1:
            duplicate_row = _semantic_match_to_row(row, [], row_id=f"semantic-training-{index}")
            duplicate_row["diagnosis_type"] = "중복 학습문장"
            duplicate_row["predicted_name"] = " / ".join(match["dialog_name"] for match in exact_matches[:3])
            duplicate_row["features"] = [match.get("matched_text", "") for match in exact_matches[:3] if match.get("matched_text")]
            items.append(_semantic_quality_item_from_row(duplicate_row))
            continue
        matches = client.search(
            bot_id=str(bot.id),
            version_id=str(version.id),
            query=row["text"],
            top_k=top_k,
            dictionary_terms=dictionary_terms,
        )
        evaluated_row = _semantic_match_to_row(row, matches, row_id=f"semantic-training-{index}")
        if evaluated_row.get("correct") is True:
            continue
        items.append(_semantic_quality_item_from_row(evaluated_row))

    status_counts: dict[str, int] = {}
    diagnosis_counts: dict[str, int] = {}
    for item in items:
        status_text = _safe_text_value(item.get("status")) or "문제"
        diagnosis_type = _safe_text_value(item.get("diagnosis_type")) or "Semantic 검색 불일치"
        status_counts[status_text] = status_counts.get(status_text, 0) + 1
        diagnosis_counts[diagnosis_type] = diagnosis_counts.get(diagnosis_type, 0) + 1
    checked_count = len(rows)
    accuracy = ((checked_count - len(items)) / checked_count * 100) if checked_count else 0.0
    return {
        "summary": {
            "engine_type": "semantic",
            "total_checked": checked_count,
            "training_checked": checked_count,
            "validation_checked": 0,
            "problem_count": len(items),
            "exact_match_count": exact_match_count,
            "accuracy": round(accuracy, 4),
            "status_counts": status_counts,
            "diagnosis_counts": diagnosis_counts,
        },
        "settings": {
            "top_k": top_k,
            "index_name": config.index_name,
            "endpoint_url": config.endpoint_url,
        },
        "items": items,
    }


def _evaluate_semantic_split(
    *,
    config,
    bot_id: str,
    version_id: str,
    index_suffix: str,
    train_rows: list[dict[str, str]],
    test_rows: list[dict[str, str]],
    top_k: int,
    dictionary_terms: list[dict[str, object]],
    exact_lookup: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[float | None, list[dict[str, object]]]:
    if not test_rows:
        return None, []
    eval_config = replace(config, index_name=f"{config.index_name}-{index_suffix}")
    eval_client = IntentVectorSearchClient(eval_config)
    eval_client.index_intents(
        bot_id=bot_id,
        version_id=version_id,
        intents=_semantic_documents_from_rows(train_rows),
        dictionary_terms=dictionary_terms,
    )
    evaluated_rows: list[dict[str, object] | None] = [None] * len(test_rows)
    pending_rows: list[tuple[int, dict[str, str]]] = []
    for index, row in enumerate(test_rows):
        if exact_lookup:
            exact_matches = exact_lookup.get(_compact_semantic_text(row["text"]), [])
            unique_by_dialog: dict[str, dict[str, str]] = {}
            for match in exact_matches:
                dialog_id = match["dialog_id"]
                if dialog_id not in unique_by_dialog:
                    unique_by_dialog[dialog_id] = match
            if len(unique_by_dialog) == 1:
                evaluated_rows[index] = _semantic_exact_match_to_row(
                    row,
                    next(iter(unique_by_dialog.values())),
                    row_id=f"{index_suffix}-{index}",
                )
                continue
        pending_rows.append((index, row))

    batch_matches = eval_client.search_batch(
        bot_id=bot_id,
        version_id=version_id,
        queries=[row["text"] for _, row in pending_rows],
        top_k=top_k,
        dictionary_terms=dictionary_terms,
    )
    for (index, row), matches in zip(pending_rows, batch_matches, strict=True):
        evaluated_rows[index] = _semantic_match_to_row(row, matches, row_id=f"{index_suffix}-{index}")

    completed_rows = [row for row in evaluated_rows if row is not None]
    return _semantic_accuracy(completed_rows), completed_rows


def _calculate_semantic_vector_evaluation(
    *,
    bot: Bot,
    version: BotVersion,
    config,
    trained_at: str,
    top_k: int,
) -> dict[str, object]:
    vector_documents = _build_intent_vector_documents(version.version_json)
    all_rows = _semantic_eval_rows_from_documents(vector_documents)
    dictionary_terms = _build_llm_dictionary_terms(version.version_json)
    fixed_train_rows, fixed_test_rows = _split_semantic_eval_rows(
        all_rows,
        seed=f"{version.id}:{trained_at}:fixed",
        randomize=False,
    )
    random_train_rows, random_test_rows = _split_semantic_eval_rows(
        all_rows,
        seed=f"{version.id}:{trained_at}:random",
        randomize=True,
    )
    fixed_exact_lookup = _semantic_exact_training_lookup(_semantic_documents_from_rows(fixed_train_rows))
    random_exact_lookup = _semantic_exact_training_lookup(_semantic_documents_from_rows(random_train_rows))
    fixed_accuracy, fixed_rows = _evaluate_semantic_split(
        config=config,
        bot_id=str(bot.id),
        version_id=str(version.id),
        index_suffix="fixed-eval",
        train_rows=fixed_train_rows,
        test_rows=fixed_test_rows,
        top_k=top_k,
        dictionary_terms=dictionary_terms,
        exact_lookup=fixed_exact_lookup,
    )
    random_accuracy, random_rows = _evaluate_semantic_split(
        config=config,
        bot_id=str(bot.id),
        version_id=str(version.id),
        index_suffix="random-eval",
        train_rows=random_train_rows,
        test_rows=random_test_rows,
        top_k=top_k,
        dictionary_terms=dictionary_terms,
        exact_lookup=random_exact_lookup,
    )
    gap = abs(random_accuracy - fixed_accuracy) if random_accuracy is not None and fixed_accuracy is not None else None
    return {
        "trained_at": trained_at,
        "engine_type": "semantic",
        "random_accuracy": random_accuracy,
        "fixed_accuracy": fixed_accuracy,
        "gap": gap,
        "intent_count": len({row["dialog_id"] for row in all_rows if row["dialog_id"]}),
        "training_utterance_count": len(all_rows),
        "validation_utterance_count": len(fixed_test_rows),
        "random_train_count": len(random_train_rows),
        "random_test_count": len(random_test_rows),
        "snapshot": {"training_rows": [*fixed_rows, *random_rows]},
        "quality_diagnostics": {
            "summary": {
                "engine_type": "semantic",
                "evaluated_count": len(fixed_rows) + len(random_rows),
                "fixed_test_count": len(fixed_rows),
                "random_test_count": len(random_rows),
            },
            "items": [row for row in [*fixed_rows, *random_rows] if row.get("correct") is not True],
        },
    }


def _train_semantic_intent_vector_index(bot: Bot, version: BotVersion, ai_config: dict[str, object]) -> dict[str, object]:
    mark_external_version_write(version.id)
    config = intent_vector_config(ai_config)
    vector_documents = _build_intent_vector_documents(version.version_json)
    if not vector_documents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Intent Vector DB에 등록할 학습문장이 없습니다. 의도에 T 학습문장을 1개 이상 등록해주세요.",
        )
    try:
        dictionary_terms = _build_llm_dictionary_terms(version.version_json)
        index_result = IntentVectorSearchClient(config).index_intents(
            bot_id=str(bot.id),
            version_id=str(version.id),
            intents=vector_documents,
            dictionary_terms=dictionary_terms,
        )
    except VectorSearchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    utterance_count = sum(len(item.get("utterances", [])) for item in vector_documents)
    trained_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "aidot-nlu-semantic-vector-v1",
        "engine_type": "semantic",
        "model": {
            "provider": "external_vector_worker",
            "index_name": config.index_name,
            "endpoint_url": config.endpoint_url,
            "trained_at": trained_at,
            "counts": {
                "intent_documents": utterance_count,
                "entity_documents": 0,
                "vocabulary": len(vector_documents),
            },
        },
        "counts": {
            "intent_count": len(vector_documents),
            "utterance_count": utterance_count,
            "indexed": int(index_result.get("indexed") or utterance_count),
        },
        "index_result": index_result,
    }


def _get_nlu_training_state(version: BotVersion) -> dict[str, object]:
    snapshot = getattr(version, "nlu_training_json", None)
    if isinstance(snapshot, dict):
        return _dict_snapshot(snapshot)
    version_json = normalize_version_document(version.version_json)
    system_config = version_json.get("system_config")
    if not isinstance(system_config, dict):
        return {}
    training = system_config.get("nlu_training")
    return training if isinstance(training, dict) else {}


def _training_audit_indicates_success(after_json: object) -> bool:
    if not isinstance(after_json, dict):
        return False
    if str(after_json.get("status") or "").lower() == "success":
        return True
    counts = after_json.get("counts")
    if not isinstance(counts, dict):
        return False
    document_count = int(counts.get("intent_documents") or 0) + int(counts.get("entity_documents") or 0)
    return document_count > 0


def _training_state_from_audit_payload(after_json: object, created_at: datetime) -> dict[str, object]:
    if not _training_audit_indicates_success(after_json):
        return {}
    payload = after_json if isinstance(after_json, dict) else {}
    return {
        "status": "success",
        "trained_at": str(payload.get("trained_at") or payload.get("completed_at") or created_at.isoformat()),
        "engine_type": str(payload.get("engine_type") or payload.get("nlu_type") or "ml"),
        "nlu_type": str(payload.get("nlu_type") or payload.get("engine_type") or "ml"),
        "nlu_model": str(payload.get("nlu_model") or payload.get("model") or "deep_learning_lite"),
        "source": "audit_log",
    }


def _get_training_state_from_audit(db: Session | None, version: BotVersion) -> dict[str, object]:
    if db is None:
        return {}
    log = db.scalar(
        select(AuditLog)
        .where(
            AuditLog.action_type == "bot.version.nlu.train",
            AuditLog.target_id == version.id,
        )
        .order_by(AuditLog.created_at.desc())
    )
    if log is None:
        return {}
    training_state = _training_state_from_audit_payload(log.after_json, log.created_at)
    if training_state and log.actor_user_id is not None:
        actor = db.get(User, log.actor_user_id)
        if actor is not None:
            training_state["trained_by_login_id"] = actor.login_id
    return training_state


def _build_version_audit_context(db: Session | None, versions: list[BotVersion]) -> dict[str, dict[UUID, object]]:
    if db is None or not versions:
        return {"training": {}, "updated_by": {}}

    version_ids = [version.id for version in versions]
    train_row_number = func.row_number().over(
        partition_by=AuditLog.target_id,
        order_by=AuditLog.created_at.desc(),
    ).label("row_number")
    train_subquery = (
        select(
            AuditLog.target_id.label("target_id"),
            AuditLog.after_json.label("after_json"),
            AuditLog.created_at.label("created_at"),
            AuditLog.actor_user_id.label("actor_user_id"),
            train_row_number,
        )
        .where(
            AuditLog.action_type == "bot.version.nlu.train",
            AuditLog.target_id.in_(version_ids),
        )
        .subquery()
    )
    training_by_version: dict[UUID, object] = {}
    training_actor_by_version: dict[UUID, UUID] = {}
    for row in db.execute(
        select(
            train_subquery.c.target_id,
            train_subquery.c.after_json,
            train_subquery.c.created_at,
            train_subquery.c.actor_user_id,
        ).where(train_subquery.c.row_number == 1)
    ):
        if row.target_id is not None:
            training_by_version[row.target_id] = _training_state_from_audit_payload(row.after_json, row.created_at)
            if row.actor_user_id is not None:
                training_actor_by_version[row.target_id] = row.actor_user_id

    update_row_number = func.row_number().over(
        partition_by=AuditLog.target_id,
        order_by=AuditLog.created_at.desc(),
    ).label("row_number")
    update_subquery = (
        select(
            AuditLog.target_id.label("target_id"),
            AuditLog.actor_user_id.label("actor_user_id"),
            update_row_number,
        )
        .where(
            AuditLog.target_type == "bot_version",
            AuditLog.target_id.in_(version_ids),
        )
        .subquery()
    )
    actor_by_version: dict[UUID, UUID] = {}
    for row in db.execute(select(update_subquery.c.target_id, update_subquery.c.actor_user_id).where(update_subquery.c.row_number == 1)):
        if row.target_id is not None and row.actor_user_id is not None:
            actor_by_version[row.target_id] = row.actor_user_id

    user_ids = set(actor_by_version.values()) | set(training_actor_by_version.values())
    users = (
        {
            user.id: user.login_id
            for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
        }
        if user_ids
        else {}
    )
    for version_id, actor_id in training_actor_by_version.items():
        training_state = training_by_version.get(version_id)
        trained_by_login_id = users.get(actor_id)
        if isinstance(training_state, dict) and trained_by_login_id:
            training_state["trained_by_login_id"] = trained_by_login_id
    updated_by_version = {
        version_id: users.get(actor_id)
        for version_id, actor_id in actor_by_version.items()
        if users.get(actor_id)
    }
    return {"training": training_by_version, "updated_by": updated_by_version}


def _is_version_trained(version: BotVersion, db: Session | None = None) -> bool:
    training = _get_nlu_training_state(version)
    if training.get("status") == "success":
        return True
    return bool(_get_training_state_from_audit(db, version))


def _bot_has_trained_version(db: Session, bot: Bot) -> bool:
    versions = db.scalars(
        select(BotVersion).where(
            BotVersion.bot_id == bot.id,
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    return any(_is_version_trained(version, db) for version in versions)


def _normalize_engine_value(key: str, value: object) -> str:
    if value is None:
        if key == "language":
            return "ko"
        if key in {"nlu_type", "engine_type"}:
            return "ml"
        if key in {"nlu_model", "nlu_engine", "model"}:
            return "deep_learning_lite"
        if key == "answer_mode":
            return "fixed"
        if key == "llm_provider":
            return "chatgpt"
        if key == "llm_model":
            return "gpt-4o-mini"
        return ""
    return str(value)


def _engine_update_changes_locked_value(current: dict[str, object], updates: dict[str, object]) -> bool:
    locked_keys = ("language", "nlu_type", "answer_mode", "nlu_engine", "nlu_model", "llm_provider", "llm_model")
    for key in locked_keys:
        if key not in updates:
            continue
        current_value = current.get(key)
        if key == "nlu_engine" and current_value is None:
            current_value = current.get("nlu_model")
        if key == "nlu_model" and current_value is None:
            current_value = current.get("nlu_engine")
        if _normalize_engine_value(key, current_value) != _normalize_engine_value(key, updates.get(key)):
            return True
    return False


def _answer_embedding_config_fingerprint(ai_config: dict[str, object]) -> tuple[str, str]:
    engine = _answer_embedding_engine_from_nlu_config(ai_config)
    if not engine:
        return ("", "")
    return (
        _safe_text_value(engine.get("embedding_provider")),
        _safe_text_value(engine.get("embedding_model")),
    )


def _hard_delete_bot_version(db: Session, version: BotVersion) -> None:
    if retains_persistent_history(db, version):
        if version.deleted_at is None:
            version.deleted_at = datetime.now(timezone.utc)
            db.add(version)
        return
    remove_unused_version_guard(db, version)
    version_room_ids = select(ChannelRoom.id).where(ChannelRoom.bot_version_id == version.id)
    db.execute(
        delete(ChannelQueueEvent).where(
            ChannelQueueEvent.bot_version_id == version.id,
        )
    )
    db.execute(
        delete(ChannelMessage).where(
            ChannelMessage.room_id.in_(version_room_ids),
        )
    )
    db.execute(
        delete(ChannelRoom).where(
            ChannelRoom.bot_version_id == version.id,
        )
    )
    db.execute(
        update(BotVersion)
        .where(BotVersion.copied_from_version_id == version.id)
        .values(copied_from_version_id=None)
    )
    db.execute(
        update(Bot)
        .where(Bot.active_version_id == version.id)
        .values(active_version_id=None)
    )
    db.delete(version)


def _purge_deleted_bot_versions(db: Session, bot_id: UUID) -> None:
    deleted_versions = db.scalars(
        select(BotVersion).where(
            BotVersion.bot_id == bot_id,
            BotVersion.deleted_at.is_not(None),
        )
    ).all()
    for version in deleted_versions:
        _hard_delete_bot_version(db, version)
    if deleted_versions:
        db.flush()


def _next_bot_version_no(db: Session, bot_id: UUID) -> int:
    _purge_deleted_bot_versions(db, bot_id)
    max_version = db.scalar(
        select(func.max(BotVersion.version_no)).where(
            BotVersion.bot_id == bot_id,
        )
    ) or 0
    return max_version + 1


def _set_nlu_training_state(version_json: dict[str, object], training_state: dict[str, object] | None) -> dict[str, object]:
    next_json = normalize_version_document(version_json)
    system_config = dict(next_json.get("system_config") or {})
    if training_state is None:
        system_config.pop("nlu_training", None)
    else:
        system_config["nlu_training"] = training_state
    next_json["system_config"] = system_config
    return next_json


def _prepare_version_document_for_save(version_json: dict[str, object], current_training_state: dict[str, object] | None = None) -> dict[str, object]:
    next_json = normalize_version_document(version_json)
    system_config = dict(next_json.get("system_config") or {})
    if "nlu_training" not in system_config and current_training_state:
        system_config["nlu_training"] = current_training_state
    next_json["system_config"] = system_config
    return attach_scenario_validation(next_json)


def _assert_full_version_update_preserves_loaded_sections(
    current_version_json: dict[str, object] | None,
    next_version_json: dict[str, object],
) -> None:
    current_document = normalize_version_document(current_version_json)
    protected_sections = {
        "dialog_flow_graphs": "대화 설계 그래프",
        "apis": "API",
    }
    for section_key, section_label in protected_sections.items():
        current_items = current_document.get(section_key)
        next_items = next_version_json.get(section_key)
        if isinstance(current_items, list) and current_items and isinstance(next_items, list) and not next_items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": f"{section_label} 데이터가 비어 있는 전체 버전 저장은 허용되지 않습니다. 해당 섹션 전용 저장 API를 사용해주세요.",
                    "section": section_key,
                },
            )


def _dict_snapshot(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _compact_system_config_summary(value: object) -> dict[str, object]:
    source = _dict_snapshot(value)
    summary = deepcopy(source)
    summary.pop("retraining_records", None)

    for key in ("nlu_evaluation", "last_bot_evaluation"):
        record = source.get(key)
        if not isinstance(record, dict):
            continue
        compact_record = {
            field: deepcopy(field_value)
            for field, field_value in record.items()
            if field_value is None or isinstance(field_value, (str, int, float, bool))
        }
        latest = record.get("latest")
        if isinstance(latest, dict):
            compact_record["latest"] = {
                field: deepcopy(field_value)
                for field, field_value in latest.items()
                if field_value is None or isinstance(field_value, (str, int, float, bool))
            }
        summary[key] = compact_record

    return summary


def _list_snapshot(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _api_asset_dedupe_key(api_asset: dict[str, object]) -> str:
    for key in ("id", "apiKey", "api_key"):
        value = str(api_asset.get(key) or "").strip()
        if value:
            return f"{key}:{value}"

    methods = api_asset.get("methods")
    method_values: list[str] = []
    if isinstance(methods, list):
        for item in methods:
            if isinstance(item, dict):
                method = str(item.get("method") or item.get("name") or "").strip().upper()
            else:
                method = str(item or "").strip().upper()
            if method:
                method_values.append(method)

    fallback_source = json.dumps(
        {
            "name": str(api_asset.get("name") or api_asset.get("apiName") or "").strip(),
            "base_url": str(api_asset.get("baseUrl") or api_asset.get("base_url") or "").strip(),
            "methods": sorted(method_values),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"fingerprint:{hashlib.sha1(fallback_source.encode('utf-8')).hexdigest()}"


def _group_api_asset_dedupe_key(api_asset: dict[str, object]) -> str:
    for key in ("apiKey", "api_key", "key"):
        value = str(api_asset.get(key) or "").strip()
        if value:
            return f"apiKey:{value}"
    return _api_asset_dedupe_key(api_asset)


def _dedupe_api_asset_list(value: object) -> list[dict[str, object]]:
    items = _list_snapshot(value)
    if not items:
        return []

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in items:
        key = _api_asset_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _dedupe_group_api_asset_list(value: object) -> list[dict[str, object]]:
    items = _list_snapshot(value)
    if not items:
        return []

    deduped: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in items:
        key = _group_api_asset_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _group_data_json(group: Group | None) -> dict[str, object]:
    return dict(group.data_json) if group is not None and isinstance(group.data_json, dict) else {}


def _group_api_assets(group: Group | None) -> list[dict[str, object]]:
    return _dedupe_group_api_asset_list(_group_data_json(group).get("apis"))


def _set_group_api_assets(group: Group, apis: list[dict[str, object]]) -> None:
    data_json = _group_data_json(group)
    data_json["apis"] = _dedupe_group_api_asset_list(apis)
    group.data_json = data_json


def _collect_group_version_api_assets(db: Session, current_user: User) -> list[dict[str, object]]:
    if current_user.group_id is None:
        return []
    bots = db.scalars(
        select(Bot)
        .where(
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == current_user.group_id,
            Bot.active_version_id.is_not(None),
            Bot.deleted_at.is_(None),
        )
    ).all()
    active_version_ids = [bot.active_version_id for bot in bots if bot.active_version_id is not None]
    if not active_version_ids:
        return []

    versions = db.scalars(
        select(BotVersion).where(
            BotVersion.id.in_(active_version_ids),
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    collected: list[dict[str, object]] = []
    for version in versions:
        document = normalize_version_document(version.version_json)
        apis = document.get("apis")
        if isinstance(apis, list):
            collected.extend(item for item in apis if isinstance(item, dict))
    return _dedupe_group_api_asset_list(collected)


def _ensure_group_api_assets(db: Session, group: Group, current_user: User) -> list[dict[str, object]]:
    existing = _group_api_assets(group)
    if existing:
        return existing

    migrated = _collect_group_version_api_assets(db, current_user)
    if not migrated:
        return []

    _set_group_api_assets(group, migrated)
    db.add(group)
    db.commit()
    db.refresh(group)
    return _group_api_assets(group)


def _group_api_reference_count(db: Session, current_user: User, api: dict[str, object]) -> int:
    api_id = str(api.get("id") or "").strip()
    api_key = str(api.get("apiKey") or api.get("api_key") or api.get("key") or "").strip()
    if not api_id and not api_key:
        return 0

    bots = db.scalars(
        select(Bot)
        .where(
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == current_user.group_id,
            Bot.active_version_id.is_not(None),
            Bot.deleted_at.is_(None),
        )
    ).all()
    active_version_ids = [bot.active_version_id for bot in bots if bot.active_version_id is not None]
    if not active_version_ids:
        return 0

    versions = db.scalars(
        select(BotVersion).where(
            BotVersion.id.in_(active_version_ids),
            BotVersion.deleted_at.is_(None),
        )
    ).all()

    def _contains_reference(value: object) -> bool:
        if not isinstance(value, (dict, list)):
            return False
        if isinstance(value, list):
            return any(_contains_reference(item) for item in value)
        candidate_id = str(value.get("apiId") or "").strip()
        candidate_key = str(value.get("apiKey") or value.get("api_key") or "").strip()
        if (api_id and candidate_id == api_id) or (api_key and candidate_key == api_key):
            return True
        return any(_contains_reference(item) for item in value.values())

    count = 0
    for version in versions:
        document = normalize_version_document(version.version_json)
        graphs = document.get("dialog_flow_graphs")
        if isinstance(graphs, list):
            count += sum(1 for graph in graphs if _contains_reference(graph))
    return count


def _version_asset_counts(version: BotVersion, version_json_override: dict[str, object] | None = None) -> dict[str, int]:
    if version_json_override is not None:
        return build_version_asset_counts(version_json_override)
    snapshot = getattr(version, "asset_counts_json", None)
    if isinstance(snapshot, dict) and snapshot:
        counts = {str(key): int(value or 0) for key, value in snapshot.items() if isinstance(value, (int, float))}
        entities_snapshot = getattr(version, "entities_json", None)
        if isinstance(entities_snapshot, list):
            counts["entities"] = sum(
                1
                for item in entities_snapshot
                if isinstance(item, dict) and not is_system_version_asset(item)
            )
        dictionary_snapshot = getattr(version, "dictionary_json", None)
        if isinstance(dictionary_snapshot, list):
            counts["dictionary"] = sum(
                1
                for item in dictionary_snapshot
                if isinstance(item, dict) and not is_system_version_asset(item)
            )
        return counts
    return build_version_asset_counts(version.version_json)


def _version_scenario_validation(version: BotVersion, version_json_override: dict[str, object] | None = None) -> dict[str, object]:
    if version_json_override is not None:
        return scenario_validation_from_version(version_json_override)
    snapshot = getattr(version, "scenario_validation_json", None)
    if isinstance(snapshot, dict):
        return _dict_snapshot(snapshot)
    return scenario_validation_from_version(version.version_json)


def _refresh_version_read_snapshot(version: BotVersion, version_json: dict[str, object]) -> None:
    normalized = normalize_version_document(version_json)
    normalized["apis"] = _dedupe_api_asset_list(normalized.get("apis"))
    system_config = normalized.get("system_config")
    version.asset_counts_json = build_version_asset_counts(normalized)
    version.scenario_validation_json = scenario_validation_from_version(normalized)
    version.nlu_training_json = _dict_snapshot(system_config.get("nlu_training") if isinstance(system_config, dict) else None)
    version.entities_json = _list_snapshot(normalized.get("entities"))
    version.dictionary_json = _list_snapshot(normalized.get("dictionary"))
    version.apis_json = _list_snapshot(normalized.get("apis"))
    version.system_config_json = _dict_snapshot(system_config)


def _assign_version_document(version: BotVersion, version_json: dict[str, object]) -> None:
    assert_active_version_write(version)
    version.version_json = version_json
    _refresh_version_read_snapshot(version, version_json)


def _float_setting(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _int_setting(value: object, default: int) -> int:
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


def _get_version_settings(bot: Bot, version: BotVersion) -> dict[str, object]:
    data_json = bot.data_json if isinstance(bot.data_json, dict) else {}
    settings_by_version = data_json.get("settings_by_version")
    if not isinstance(settings_by_version, dict):
        return {}

    # Version IDs are immutable. Keep the former name-key lookup only for existing data.
    version_id = str(getattr(version, "id", "") or "")
    version_name = str(getattr(version, "name", "") or "")
    for key in (version_id, version_name):
        candidate = settings_by_version.get(key)
        if isinstance(candidate, dict):
            return candidate
    return {}


def _get_ml_settings(bot: Bot, version: BotVersion) -> dict[str, object]:
    version_settings = _get_version_settings(bot, version)
    conversation_defaults = version_settings.get("conversationDefaults") if isinstance(version_settings, dict) else {}
    ml_settings = conversation_defaults.get("ml") if isinstance(conversation_defaults, dict) else {}
    return ml_settings if isinstance(ml_settings, dict) else {}


def _get_validation_settings(bot: Bot, version: BotVersion) -> dict[str, object]:
    version_settings = _get_version_settings(bot, version)
    conversation_defaults = version_settings.get("conversationDefaults") if isinstance(version_settings, dict) else {}
    validation_settings = conversation_defaults.get("validation") if isinstance(conversation_defaults, dict) else {}
    return validation_settings if isinstance(validation_settings, dict) else {}


def _get_bot_or_404(db: Session, bot_id: UUID, user: User) -> Bot:
    if user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 봇에 접근할 수 있습니다.",
        )
    bot = db.scalar(
        select(Bot).where(
            Bot.id == bot_id,
            Bot.organization_id == user.organization_id,
            Bot.group_id == user.group_id,
            Bot.created_by == user.id,
            Bot.deleted_at.is_(None),
        )
    )
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="봇을 찾을 수 없습니다.",
        )
    return bot


def _get_version_or_404(db: Session, bot_id: UUID, version_id: UUID) -> BotVersion:
    version = db.scalar(
        select(BotVersion).where(
            BotVersion.id == version_id,
            BotVersion.bot_id == bot_id,
            BotVersion.deleted_at.is_(None),
        )
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="버전을 찾을 수 없습니다.",
        )
    admit_version_write(db, version)
    return version


def _get_version_by_scope(db: Session, bot_id: UUID, scope: str) -> BotVersion | None:
    scope = scope.strip()
    version_id: UUID | None = None
    try:
        version_id = UUID(scope)
    except ValueError:
        version_id = None

    conditions = [BotVersion.name == scope]
    if version_id is not None:
        conditions.append(BotVersion.id == version_id)

    return db.scalar(
        select(BotVersion).where(
            BotVersion.bot_id == bot_id,
            BotVersion.deleted_at.is_(None),
            or_(*conditions),
        )
    )


def _list_bot_versions(
    db: Session,
    bot_id: UUID,
    *,
    include_document: bool,
) -> list[BotVersion]:
    query = select(BotVersion).where(
        BotVersion.bot_id == bot_id,
        BotVersion.deleted_at.is_(None),
    ).order_by(BotVersion.version_no.desc())
    if not include_document:
        query = query.options(defer(BotVersion.version_json))
    return db.scalars(query).all()


def _public_hub_payload(bot: Bot, payload: object) -> object:
    """허브 파생 학습자료의 공개 투영이며 내부 저장·시나리오는 수정하지 않는다."""
    if (getattr(bot, "data_json", None) or {}).get("bot_kind") != "hub":
        # 종류 표시 변경으로 우회하지 못하도록 기존 서버 허브 레코드도 확인한다.
        session = object_session(bot) if isinstance(bot, Bot) else None
        if session is None or session.get(BotHub, bot.id) is None:
            return payload
    scalar_metadata = {"status", "trained_at", "trained_by_login_id", "provider", "model", "engine_type",
                       "nlu_type", "nlu_model", "nlu_engine", "schema_version", "started_at", "completed_at",
                       "morph_analyzer", "language", "bot_id", "version_id"}

    def metadata(value: dict) -> dict:
        result = {key: item for key, item in value.items()
                  if isinstance(item, (int, float, bool))
                  or (key in scalar_metadata and isinstance(item, (str, type(None))))}
        if isinstance(value.get("counts"), dict):
            result["counts"] = {key: item for key, item in value["counts"].items()
                                if isinstance(item, (int, float, bool))}
        return result

    def dialog(value: object) -> object:
        if not isinstance(value, dict) or not str(value.get("id") or "").startswith("hub-member-"):
            return deepcopy(value)
        return {**deepcopy(value), "utterances": []}

    def system_config(value: dict) -> dict:
        result = deepcopy(value)
        training = result.get("nlu_training")
        if isinstance(training, dict):
            result["nlu_training"] = metadata(training)
        evaluation = result.get("nlu_evaluation")
        if isinstance(evaluation, dict):
            summary = metadata(evaluation)
            if isinstance(evaluation.get("latest"), dict):
                summary["latest"] = metadata(evaluation["latest"])
            if isinstance(evaluation.get("history"), list):
                summary["history"] = [metadata(item) for item in evaluation["history"] if isinstance(item, dict)]
            result["nlu_evaluation"] = summary
        return result

    def visible(value: object) -> object:
        if not isinstance(value, dict):
            return deepcopy(value)
        result = deepcopy(value)
        result.pop("_aidot_package_compatibility", None)
        for key in ("version_json", "version", "manifest"):
            if isinstance(result.get(key), dict):
                result[key] = visible(result[key])
        if isinstance(result.get("versions"), list):
            result["versions"] = [visible(item) for item in result["versions"]]
        if isinstance(result.get("system_config"), dict):
            result["system_config"] = system_config(result["system_config"])
        if isinstance(result.get("dialogs"), list):
            result["dialogs"] = [dialog(item) for item in result["dialogs"]]
        if isinstance(result.get("dialog"), dict):
            result["dialog"] = dialog(result["dialog"])
        if result.get("section") == "dialogs" and isinstance(result.get("items"), list):
            result["items"] = [dialog(item) for item in result["items"]]
        for key in ("model", "nlu_training", "evaluation", "snapshot", "quality_diagnostics", "training_quality_diagnostics"):
            if isinstance(result.get(key), dict):
                result[key] = metadata(result[key])
        if isinstance(result.get("candidates"), list):
            result["candidates"] = [{**item, "reason": ""} if isinstance(item, dict) else item for item in result["candidates"]]
            if "raw_content" in result:
                result["raw_content"] = ""
        if result.get("job_id") and result.get("error_message"):
            result["error_message"] = "허브 학습 처리에 실패했습니다. 관리자에게 확인해 주세요."
        return result

    return visible(payload)


def _serialize_version(
    bot: Bot,
    version: BotVersion,
    db: Session | None = None,
    audit_context: dict[str, dict[UUID, object]] | None = None,
) -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)
    scenario_validation = scenario_validation_from_version(version_json)
    local_training = _get_nlu_training_state(version)
    audit_training = {}
    if not local_training:
        audit_training = (
            audit_context.get("training", {}).get(version.id, {}) if audit_context is not None else _get_training_state_from_audit(db, version)
        )
    nlu_training = local_training or (audit_training if isinstance(audit_training, dict) else {})
    updated_by_login_id = (
        audit_context.get("updated_by", {}).get(version.id) if audit_context is not None else None
    )
    if db is not None and audit_context is None and not local_training:
        latest_log = db.scalar(
            select(AuditLog)
            .where(AuditLog.target_type == "bot_version", AuditLog.target_id == version.id)
            .order_by(AuditLog.created_at.desc())
        )
        if latest_log is not None and latest_log.actor_user_id is not None:
            actor = db.get(User, latest_log.actor_user_id)
            updated_by_login_id = actor.login_id if actor is not None else None
    if local_training and not local_training.get("trained_by_login_id") and audit_context is not None:
        audit_training_candidate = audit_context.get("training", {}).get(version.id, {})
        if isinstance(audit_training_candidate, dict) and audit_training_candidate.get("trained_by_login_id"):
            nlu_training = {**local_training, "trained_by_login_id": audit_training_candidate["trained_by_login_id"]}
    if not updated_by_login_id and isinstance(nlu_training, dict):
        trained_by_login_id = nlu_training.get("trained_by_login_id")
        if isinstance(trained_by_login_id, str) and trained_by_login_id.strip():
            updated_by_login_id = trained_by_login_id.strip()
    return _public_hub_payload(bot, {
        "id": str(version.id),
        "bot_id": str(version.bot_id),
        "version_no": version.version_no,
        "name": version.name,
        "description": version.description,
        "status": version.status,
        "comment": version.comment,
        "version_json": version_json,
        "asset_counts": _version_asset_counts(version, version_json),
        "scenario_validation": scenario_validation,
        "is_active": bot.active_version_id == version.id,
        "copied_from_version_id": str(version.copied_from_version_id) if version.copied_from_version_id else None,
        "activated_at": _iso(version.activated_at),
        "is_trained": local_training.get("status") == "success" or bool(nlu_training),
        "nlu_training": nlu_training,
        "updated_by_login_id": str(updated_by_login_id) if updated_by_login_id else None,
        "updated_at": _iso(version.updated_at),
        "created_at": _iso(version.created_at),
    })


def _serialize_version_summary(
    bot: Bot,
    version: BotVersion,
    db: Session | None = None,
    version_json_override: dict[str, object] | None = None,
    audit_context: dict[str, dict[UUID, object]] | None = None,
) -> dict[str, object]:
    version_json = version_json_override
    system_config_snapshot = getattr(version, "system_config_json", None)
    if not isinstance(system_config_snapshot, dict) and isinstance(version_json, dict):
        system_config_snapshot = version_json.get("system_config")
    if not isinstance(system_config_snapshot, dict):
        system_config_snapshot = {}
    local_training = _get_nlu_training_state(version)
    audit_training = {}
    if not local_training:
        audit_training = (
            audit_context.get("training", {}).get(version.id, {}) if audit_context is not None else _get_training_state_from_audit(db, version)
        )
    nlu_training = local_training or (audit_training if isinstance(audit_training, dict) else {})
    if local_training and not local_training.get("trained_by_login_id") and audit_context is not None:
        audit_training_candidate = audit_context.get("training", {}).get(version.id, {})
        if isinstance(audit_training_candidate, dict) and audit_training_candidate.get("trained_by_login_id"):
            nlu_training = {**local_training, "trained_by_login_id": audit_training_candidate["trained_by_login_id"]}
    updated_by_login_id = (
        audit_context.get("updated_by", {}).get(version.id) if audit_context is not None else None
    )
    if db is not None and audit_context is None and not local_training:
        latest_log = db.scalar(
            select(AuditLog)
            .where(AuditLog.target_type == "bot_version", AuditLog.target_id == version.id)
            .order_by(AuditLog.created_at.desc())
        )
        if latest_log is not None and latest_log.actor_user_id is not None:
            actor = db.get(User, latest_log.actor_user_id)
            updated_by_login_id = actor.login_id if actor is not None else None
    if not updated_by_login_id and isinstance(nlu_training, dict):
        trained_by_login_id = nlu_training.get("trained_by_login_id")
        if isinstance(trained_by_login_id, str) and trained_by_login_id.strip():
            updated_by_login_id = trained_by_login_id.strip()
    return _public_hub_payload(bot, {
        "id": str(version.id),
        "bot_id": str(version.bot_id),
        "version_no": version.version_no,
        "name": version.name,
        "description": version.description,
        "status": version.status,
        "comment": version.comment,
        "system_config": _compact_system_config_summary(system_config_snapshot),
        "asset_counts": _version_asset_counts(version, version_json),
        "scenario_validation": _version_scenario_validation(version, version_json),
        "is_active": bot.active_version_id == version.id,
        "copied_from_version_id": str(version.copied_from_version_id) if version.copied_from_version_id else None,
        "activated_at": _iso(version.activated_at),
        "is_trained": local_training.get("status") == "success" or bool(nlu_training),
        "nlu_training": nlu_training,
        "updated_by_login_id": str(updated_by_login_id) if updated_by_login_id else None,
        "updated_at": _iso(version.updated_at),
        "created_at": _iso(version.created_at),
    })


def _serialize_version_settings_summary(
    bot: Bot,
    version: BotVersion,
    db: Session | None = None,
    *,
    include_ai_config: bool = False,
    include_training: bool = False,
    include_dialogs: bool = False,
) -> dict[str, object]:
    should_include_training = include_ai_config or include_training
    local_training = _get_nlu_training_state(version) if should_include_training else {}
    audit_training = {} if local_training or not should_include_training else _get_training_state_from_audit(db, version)
    nlu_training = local_training or (audit_training if isinstance(audit_training, dict) else {})
    if should_include_training and local_training and not local_training.get("trained_by_login_id"):
        audit_training_candidate = {}
        if isinstance(audit_training_candidate, dict) and audit_training_candidate.get("trained_by_login_id"):
            nlu_training = {**local_training, "trained_by_login_id": audit_training_candidate["trained_by_login_id"]}
    payload: dict[str, object] = {
        "id": str(version.id),
        "bot_id": str(version.bot_id),
        "version_no": version.version_no,
        "name": version.name,
        "description": version.description,
        "status": version.status,
        "comment": version.comment,
        "system_config": _dict_snapshot(getattr(version, "system_config_json", None)),
        "is_active": bot.active_version_id == version.id,
        "copied_from_version_id": str(version.copied_from_version_id) if version.copied_from_version_id else None,
        "activated_at": _iso(version.activated_at),
        "is_trained": local_training.get("status") == "success" or bool(nlu_training),
        "nlu_training": nlu_training,
        "updated_at": _iso(version.updated_at),
        "created_at": _iso(version.created_at),
    }
    version_json_payload = _dict_snapshot(getattr(version, "version_json", None))
    version_payload: dict[str, object] = {}
    if include_ai_config:
        version_system_config = _dict_snapshot(getattr(version, "system_config_json", None))
        if not isinstance(version_system_config, dict):
            version_system_config = {}
        version_payload["system_config"] = {
            **version_system_config,
            "ai_config": _get_version_ai_config(bot, version),
            "nlu_training": nlu_training,
        }
    if include_dialogs:
        dialogs = version_json_payload.get("dialogs")
        if isinstance(dialogs, list):
            version_payload["dialogs"] = deepcopy(dialogs)
    if version_payload:
        payload["version_json"] = {
            **version_payload,
        }
    return _public_hub_payload(bot, payload)


def _serialize_bot_settings_summary(
    db: Session,
    bot: Bot,
    *,
    active_version_override: BotVersion | None = None,
    version_count_override: int | None = None,
) -> dict[str, object]:
    group = db.scalar(select(Group).where(Group.id == bot.group_id))
    active_version = active_version_override
    if active_version is None and bot.active_version_id:
        active_version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.deleted_at.is_(None),
            )
        )
    version_count = version_count_override
    if version_count is None:
        version_count = db.scalar(
            select(func.count(BotVersion.id)).where(
                BotVersion.bot_id == bot.id,
                BotVersion.deleted_at.is_(None),
            )
        ) or 0

    return {
        "id": str(bot.id),
        "organization_id": str(bot.organization_id),
        "group_id": str(bot.group_id),
        "group_code": group.code if group is not None else None,
        "group_name": group.name if group is not None else None,
        "name": bot.name,
        "description": bot.description,
        "status": bot.status,
        "data_json": bot.data_json or {},
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
        "active_version": _serialize_version_settings_summary(
            bot,
            active_version,
            db,
            include_ai_config=True,
            include_dialogs=True,
        ) if active_version else None,
        "version_count": version_count,
        "updated_at": _iso(bot.updated_at),
        "created_at": _iso(bot.created_at),
    }


def _serialize_version_dialogs(bot: Bot, version: BotVersion, db: Session | None = None) -> dict[str, object]:
    return _serialize_version_document_items(bot, version, "dialogs", db)


def _version_cache_key(version: BotVersion, section: str, extra: str = "") -> str:
    updated_at = _iso(version.updated_at) or "unknown"
    suffix = f":{extra}" if extra else ""
    return f"version:v2:{version.id}:{updated_at}:{section}{suffix}"


def _version_collection_cache_stamp(bot: Bot, versions: list[BotVersion]) -> str:
    bot_stamp = _iso(bot.updated_at) or "unknown"
    version_stamps = [
        ":".join(
            [
                str(version.id),
                str(version.version_no),
                version.name,
                version.status,
                _iso(version.updated_at) or "unknown",
                "active" if bot.active_version_id == version.id else "inactive",
            ]
        )
        for version in versions
    ]
    return "|".join([str(bot.id), bot_stamp, *version_stamps])


def _cache_stamp_digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _cached_version_payload(version: BotVersion, section: str, producer, extra: str = ""):
    return cache_aside_json(
        _version_cache_key(version, section, extra),
        producer,
        ttl_seconds=settings.cache_version_section_ttl_seconds,
    )


def _purge_version_cache(version: BotVersion) -> dict[str, object]:
    return purge_cache_pattern(f"version:{version.id}:*")


def _active_version_dialog_payloads(db: Session | None, version: BotVersion) -> list[dict[str, object]] | None:
    if db is None:
        return None
    try:
        rows = db.scalars(
            select(VersionDialogAsset)
            .where(
                VersionDialogAsset.version_id == version.id,
                VersionDialogAsset.deleted_at.is_(None),
            )
            .order_by(VersionDialogAsset.sort_order.asc(), VersionDialogAsset.created_at.asc())
        ).all()
    except (OperationalError, ProgrammingError):
        db.rollback()
        return None
    if not rows:
        return None
    return [deepcopy(row.payload_json) for row in rows if isinstance(row.payload_json, dict)]


def _active_version_dialog_flow_payloads(db: Session | None, version: BotVersion) -> list[dict[str, object]] | None:
    if db is None:
        return None
    try:
        rows = db.scalars(
            select(VersionDialogFlowGraph)
            .where(
                VersionDialogFlowGraph.version_id == version.id,
                VersionDialogFlowGraph.deleted_at.is_(None),
            )
            .order_by(VersionDialogFlowGraph.created_at.asc())
        ).all()
    except (OperationalError, ProgrammingError):
        db.rollback()
        return None
    if not rows:
        return None
    return [deepcopy(row.payload_json) for row in rows if isinstance(row.payload_json, dict)]


def _version_document_for_dialog_reads(version: BotVersion, db: Session | None = None) -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)

    dialog_payloads = _active_version_dialog_payloads(db, version)
    if dialog_payloads is not None:
        version_json["dialogs"] = dialog_payloads

    graph_payloads = _active_version_dialog_flow_payloads(db, version)
    if graph_payloads is not None:
        version_json["dialog_flow_graphs"] = graph_payloads

    return version_json


def _version_document_for_light_dialog_list(version: BotVersion, db: Session | None = None) -> dict[str, object] | None:
    if getattr(version, "asset_counts_json", None) is None or getattr(version, "scenario_validation_json", None) is None:
        return None

    dialog_payloads = _active_version_dialog_payloads(db, version)
    if dialog_payloads is None:
        return None

    version_json = build_default_version_document()
    version_json["dialogs"] = dialog_payloads
    graph_payloads = _active_version_dialog_flow_payloads(db, version)
    if graph_payloads is not None:
        version_json["dialog_flow_graphs"] = graph_payloads
    return version_json


def _version_document_for_section_snapshot(version: BotVersion, section_key: str) -> dict[str, object] | None:
    snapshot_attr = {
        "entities": "entities_json",
        "dictionary": "dictionary_json",
        "apis": "apis_json",
    }.get(section_key)
    if snapshot_attr is None:
        return None
    section_snapshot = getattr(version, snapshot_attr, None)
    if not isinstance(section_snapshot, list):
        return None

    version_json = build_default_version_document()
    version_json[section_key] = (
        _dedupe_api_asset_list(section_snapshot)
        if section_key == "apis"
        else _list_snapshot(section_snapshot)
    )
    return version_json


def _version_document_for_snapshot_read(
    version: BotVersion,
    db: Session | None = None,
    *,
    include_dialogs: bool = False,
    include_dialog_flow_graphs: bool = False,
    include_entities: bool = False,
    include_dictionary: bool = False,
    include_apis: bool = False,
    include_system_config: bool = False,
) -> dict[str, object] | None:
    version_json = build_default_version_document()

    section_attrs = {
        "entities": ("entities_json", include_entities),
        "dictionary": ("dictionary_json", include_dictionary),
        "apis": ("apis_json", include_apis),
    }
    for section_key, (snapshot_attr, should_include) in section_attrs.items():
        if not should_include:
            continue
        section_snapshot = getattr(version, snapshot_attr, None)
        if not isinstance(section_snapshot, list):
            return None
        version_json[section_key] = (
            _dedupe_api_asset_list(section_snapshot)
            if section_key == "apis"
            else _list_snapshot(section_snapshot)
        )

    if include_system_config:
        system_config_snapshot = getattr(version, "system_config_json", None)
        if not isinstance(system_config_snapshot, dict):
            return None
        version_json["system_config"] = _dict_snapshot(system_config_snapshot)

    if include_dialogs:
        dialog_payloads = _active_version_dialog_payloads(db, version)
        if dialog_payloads is None:
            return None
        version_json["dialogs"] = dialog_payloads

    if include_dialog_flow_graphs:
        graph_payloads = _active_version_dialog_flow_payloads(db, version)
        if graph_payloads is None:
            return None
        version_json["dialog_flow_graphs"] = graph_payloads

    return version_json


def _serialize_version_document_items(
    bot: Bot,
    version: BotVersion,
    section_key: str,
    db: Session | None = None,
) -> dict[str, object]:
    light_dialog_list = _version_document_for_light_dialog_list(version, db) if section_key == "dialogs" else None
    section_snapshot = None if light_dialog_list is not None else _version_document_for_section_snapshot(version, section_key)
    light_document = light_dialog_list or section_snapshot
    version_json = light_document or (
        _version_document_for_dialog_reads(version, db)
        if section_key == "dialogs"
        else normalize_version_document(version.version_json)
    )
    summary_document = None if light_document is not None else version_json
    items = version_json.get(section_key)
    return _public_hub_payload(bot, {
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "section": section_key,
        "version": _serialize_version_summary(bot, version, db, summary_document),
        "items": deepcopy(items if isinstance(items, list) else []),
        "asset_counts": _version_asset_counts(version, summary_document),
        "scenario_validation": _version_scenario_validation(version, summary_document),
        "updated_at": _iso(version.updated_at),
    })


def _version_document_with_dialogs(
    version: BotVersion,
    dialogs: list[dict[str, object]],
    db: Session | None = None,
) -> dict[str, object]:
    if db is None:
        return _version_document_with_items(version, "dialogs", dialogs)

    version_json = _version_document_for_dialog_reads(version, db)
    version_json["dialogs"] = deepcopy(dialogs)
    return _prepare_version_document_for_save(version_json, _get_nlu_training_state(version))


def _assert_dialog_not_locked_by_other(db: Session, version: BotVersion, dialog_id: str, current_user: User) -> None:
    lock = db.scalar(
        select(EditLock).where(
            EditLock.bot_id == version.bot_id,
            EditLock.version_id == version.id,
            EditLock.dialog_id == dialog_id,
            EditLock.area == "dialog",
        )
    )
    now = datetime.now(timezone.utc)
    if lock is None or lock.released_at is not None or lock.expires_at <= now or lock.owner_user_id == current_user.id:
        return

    owner_label = lock.owner_name or lock.owner_login_id or "다른 사용자"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": f"현재 {owner_label}님이 이 대화를 편집 중입니다. 조회 모드로 다시 열어주세요.",
            "lock": {
                "lock_id": str(lock.id),
                "owner_login_id": lock.owner_login_id,
                "owner_name": lock.owner_name,
                "expires_at": lock.expires_at.isoformat(),
            },
        },
    )


def _assert_dialog_updates_not_locked_by_other(
    db: Session,
    version: BotVersion,
    next_dialogs: list[dict[str, object]],
    current_user: User,
) -> None:
    current_document = normalize_version_document(version.version_json)
    current_by_id = {
        str(item.get("id") or ""): item
        for item in current_document["dialogs"]
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    next_by_id = {
        str(item.get("id") or ""): item
        for item in next_dialogs
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    changed_dialog_ids = {
        dialog_id
        for dialog_id, next_dialog in next_by_id.items()
        if dialog_id in current_by_id and current_by_id[dialog_id] != next_dialog
    }
    changed_dialog_ids.update(dialog_id for dialog_id in current_by_id if dialog_id not in next_by_id)
    for dialog_id in sorted(changed_dialog_ids):
        _assert_dialog_not_locked_by_other(db, version, dialog_id, current_user)


def _version_document_with_items(
    version: BotVersion,
    section_key: str,
    items: list[dict[str, object]],
) -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)
    version_json[section_key] = (
        _dedupe_api_asset_list(items)
        if section_key == "apis"
        else deepcopy(items)
    )
    return _prepare_version_document_for_save(version_json, _get_nlu_training_state(version))


def _serialize_version_reference_items(bot: Bot, version: BotVersion, db: Session | None = None) -> dict[str, object]:
    version_json = _version_document_for_dialog_reads(version, db)
    return _public_hub_payload(bot, {
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "version": _serialize_version_summary(bot, version, db, version_json),
        "dialogs": deepcopy(version_json["dialogs"]),
        "dialog_flow_graphs": deepcopy(version_json["dialog_flow_graphs"]),
        "rules": deepcopy(version_json["rules"]),
        "updated_at": _iso(version.updated_at),
    })


def _find_dialog_document(version_json: dict[str, object], dialog_id: str) -> dict[str, object] | None:
    for dialog in version_json["dialogs"]:
        if isinstance(dialog, dict) and str(dialog.get("id") or "") == dialog_id:
            return deepcopy(dialog)
    return None


def _find_dialog_flow_graph(version_json: dict[str, object], dialog_id: str) -> dict[str, object] | None:
    for graph in version_json["dialog_flow_graphs"]:
        if isinstance(graph, dict) and str(graph.get("dialogId") or "") == dialog_id:
            return deepcopy(graph)
    return None


def _serialize_version_dialog_flow(
    bot: Bot,
    version: BotVersion,
    dialog_id: str,
    db: Session | None = None,
) -> dict[str, object]:
    light_document = _version_document_for_snapshot_read(
        version,
        db,
        include_dialogs=True,
        include_dialog_flow_graphs=True,
        include_entities=True,
        include_apis=True,
        include_system_config=True,
    )
    version_json = light_document or _version_document_for_dialog_reads(version, db)
    summary_document = None if light_document is not None else version_json
    dialog = _find_dialog_document(version_json, dialog_id)
    if dialog is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialog not found")

    graph = _find_dialog_flow_graph(version_json, dialog_id)
    return _public_hub_payload(bot, {
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "dialog_id": dialog_id,
        "version": _serialize_version_summary(bot, version, db, summary_document),
        "dialog": dialog,
        "dialogs": deepcopy(version_json["dialogs"]),
        "dialog_flow_graphs": [graph] if graph is not None else [],
        "entities": deepcopy(version_json["entities"]),
        "apis": deepcopy(version_json["apis"]),
        "system_config": deepcopy(version_json["system_config"]),
        "asset_counts": _version_asset_counts(version, summary_document),
        "scenario_validation": _version_scenario_validation(version, summary_document),
        "updated_at": _iso(version.updated_at),
    })


def _version_document_with_dialog_flow(
    version: BotVersion,
    dialog_id: str,
    dialog: dict[str, object],
    graph: dict[str, object],
) -> dict[str, object]:
    if str(dialog.get("id") or "") != dialog_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dialog id does not match path")
    if str(graph.get("dialogId") or "") != dialog_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Graph dialogId does not match path")

    version_json = normalize_version_document(version.version_json)
    next_dialogs: list[dict[str, object]] = []
    replaced_dialog = False
    for item in version_json["dialogs"]:
        if isinstance(item, dict) and str(item.get("id") or "") == dialog_id:
            next_dialogs.append(deepcopy(dialog))
            replaced_dialog = True
        else:
            next_dialogs.append(deepcopy(item))

    if not replaced_dialog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dialog not found")

    next_graphs = [
        deepcopy(item)
        for item in version_json["dialog_flow_graphs"]
        if not (isinstance(item, dict) and str(item.get("dialogId") or "") == dialog_id)
    ]
    next_graphs.append(deepcopy(graph))
    version_json["dialogs"] = next_dialogs
    version_json["dialog_flow_graphs"] = next_graphs
    return _prepare_version_document_for_save(version_json, _get_nlu_training_state(version))


def _serialize_version_configure(
    bot: Bot,
    version: BotVersion,
    db: Session | None = None,
    *,
    include_dialog_flow_graphs: bool = True,
) -> dict[str, object]:
    light_document = _version_document_for_snapshot_read(
        version,
        db,
        include_dialogs=True,
        include_dialog_flow_graphs=include_dialog_flow_graphs,
        include_dictionary=True,
        include_entities=True,
        include_system_config=True,
    )
    version_json = light_document or _version_document_for_dialog_reads(version, db)
    summary_document = None if light_document is not None else version_json
    return _public_hub_payload(bot, {
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "version": _serialize_version_summary(bot, version, db, summary_document),
        "dialogs": deepcopy(version_json["dialogs"]),
        "dialog_flow_graphs": deepcopy(version_json["dialog_flow_graphs"]) if include_dialog_flow_graphs else [],
        "dictionary": deepcopy(version_json["dictionary"]),
        "entities": deepcopy(version_json["entities"]),
        "system_config": deepcopy(version_json["system_config"]),
        "asset_counts": _version_asset_counts(version, summary_document),
        "scenario_validation": _version_scenario_validation(version, summary_document),
        "updated_at": _iso(version.updated_at),
    })


def _version_document_with_configure(
    version: BotVersion,
    dialogs: list[dict[str, object]],
    dialog_flow_graphs: list[dict[str, object]],
) -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)
    version_json["dialogs"] = deepcopy(dialogs)
    version_json["dialog_flow_graphs"] = deepcopy(dialog_flow_graphs)
    return _prepare_version_document_for_save(version_json, _get_nlu_training_state(version))


def _serialize_version_retraining(bot: Bot, version: BotVersion, db: Session | None = None) -> dict[str, object]:
    light_document = _version_document_for_snapshot_read(
        version,
        db,
        include_dialogs=True,
        include_system_config=True,
    )
    version_json = light_document or _version_document_for_dialog_reads(version, db)
    summary_document = None if light_document is not None else version_json
    return _public_hub_payload(bot, {
        "bot_id": str(bot.id),
        "version_id": str(version.id),
        "version": _serialize_version_summary(bot, version, db, summary_document),
        "dialogs": deepcopy(version_json["dialogs"]),
        "system_config": deepcopy(version_json["system_config"]),
        "asset_counts": _version_asset_counts(version, summary_document),
        "scenario_validation": _version_scenario_validation(version, summary_document),
        "updated_at": _iso(version.updated_at),
    })


def _version_document_with_retraining(
    version: BotVersion,
    dialogs: list[dict[str, object]],
    system_config: dict[str, object],
) -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)
    version_json["dialogs"] = deepcopy(dialogs)
    version_json["system_config"] = deepcopy(system_config)
    return _prepare_version_document_for_save(version_json, _get_nlu_training_state(version))


def _serialize_bot(
    db: Session,
    bot: Bot,
    *,
    active_version_override: BotVersion | None = None,
    version_count_override: int | None = None,
    audit_context: dict[str, dict[UUID, object]] | None = None,
) -> dict[str, object]:
    group = db.scalar(select(Group).where(Group.id == bot.group_id))
    active_version = active_version_override
    if active_version is None and bot.active_version_id:
        active_version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.deleted_at.is_(None),
            )
        )

    version_count = version_count_override
    if version_count is None:
        version_count = db.scalar(
            select(func.count(BotVersion.id)).where(
                BotVersion.bot_id == bot.id,
                BotVersion.deleted_at.is_(None),
            )
        ) or 0

    creator = db.get(User, bot.created_by) if bot.created_by else None

    return {
        "id": str(bot.id),
        "organization_id": str(bot.organization_id),
        "group_id": str(bot.group_id),
        "group_code": group.code if group is not None else None,
        "group_name": group.name if group is not None else None,
        "name": bot.name,
        "description": bot.description,
        "status": bot.status,
        "data_json": bot.data_json or {},
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
        "active_version": _serialize_version(bot, active_version, db, audit_context) if active_version else None,
        "version_count": version_count,
        "created_by": str(bot.created_by) if bot.created_by else None,
        "creator_login_id": creator.login_id if creator is not None else None,
        "creator_name": creator.name if creator is not None else None,
        "updated_at": _iso(bot.updated_at),
        "created_at": _iso(bot.created_at),
    }


def _serialize_bot_summary(
    db: Session,
    bot: Bot,
    *,
    active_version_override: BotVersion | None = None,
    version_count_override: int | None = None,
    audit_context: dict[str, dict[UUID, object]] | None = None,
    group_override: Group | None = None,
) -> dict[str, object]:
    group = group_override or db.scalar(select(Group).where(Group.id == bot.group_id))
    active_version = active_version_override
    if active_version is None and bot.active_version_id:
        active_version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.deleted_at.is_(None),
            )
        )
    version_count = version_count_override
    if version_count is None:
        version_count = db.scalar(
            select(func.count(BotVersion.id)).where(
                BotVersion.bot_id == bot.id,
                BotVersion.deleted_at.is_(None),
            )
        ) or 0

    creator = db.get(User, bot.created_by) if bot.created_by else None

    return {
        "id": str(bot.id),
        "organization_id": str(bot.organization_id),
        "group_id": str(bot.group_id),
        "group_code": group.code if group is not None else None,
        "group_name": group.name if group is not None else None,
        "name": bot.name,
        "description": bot.description,
        "status": bot.status,
        "data_json": bot.data_json or {},
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
        "active_version": _serialize_version_summary(bot, active_version, db, audit_context=audit_context) if active_version else None,
        "version_count": version_count,
        "created_by": str(bot.created_by) if bot.created_by else None,
        "creator_login_id": creator.login_id if creator is not None else None,
        "creator_name": creator.name if creator is not None else None,
        "updated_at": _iso(bot.updated_at),
        "created_at": _iso(bot.created_at),
    }


def _contains_api_reference(value: object, api_id: str) -> bool:
    if isinstance(value, list):
        return any(_contains_api_reference(item, api_id) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("apiId") == api_id:
        return True
    return any(_contains_api_reference(item, api_id) for item in value.values())


def _api_catalog_usage_counts(apis: list[dict[str, object]], dialog_flow_graphs: list[dict[str, object]]) -> dict[str, int]:
    api_ids = [api.get("id") for api in apis if isinstance(api.get("id"), str)]
    return {
        str(api_id): sum(1 for graph in dialog_flow_graphs if _contains_api_reference(graph, str(api_id)))
        for api_id in api_ids
    }


def _version_document_for_api_catalog(version: BotVersion, db: Session | None = None) -> dict[str, object] | None:
    apis_snapshot = getattr(version, "apis_json", None)
    if not isinstance(apis_snapshot, list):
        return None

    graph_payloads = _active_version_dialog_flow_payloads(db, version)
    if graph_payloads is None:
        return None

    version_json = build_default_version_document()
    version_json["apis"] = _dedupe_api_asset_list(apis_snapshot)
    version_json["dialog_flow_graphs"] = graph_payloads
    return version_json


def _serialize_bot_api_catalog_summary(
    db: Session,
    bot: Bot,
    *,
    active_version_override: BotVersion | None = None,
    version_count_override: int | None = None,
    audit_context: dict[str, dict[UUID, object]] | None = None,
) -> dict[str, object]:
    data = _serialize_bot_summary(
        db,
        bot,
        active_version_override=active_version_override,
        version_count_override=version_count_override,
        audit_context=audit_context,
    )
    active_version = active_version_override
    if active_version is None and bot.active_version_id:
        active_version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.deleted_at.is_(None),
            )
        )
    active_version_data = data.get("active_version")
    if active_version is None or not isinstance(active_version_data, dict):
        return data

    version_json = _version_document_for_api_catalog(active_version, db) or normalize_version_document(active_version.version_json)
    usage_counts = _api_catalog_usage_counts(version_json["apis"], version_json["dialog_flow_graphs"])
    apis: list[dict[str, object]] = []
    for api in version_json["apis"]:
        api_id = api.get("id")
        next_api = deepcopy(api)
        next_api["usageCount"] = usage_counts.get(str(api_id), 0) if isinstance(api_id, str) else 0
        apis.append(next_api)
    active_version_data["version_json"] = {
        "asset_format_version": version_json["asset_format_version"],
        "apis": apis,
        "dialog_flow_graphs": [],
    }
    data["active_version"] = active_version_data
    return data


def _matches_version_scope(version: BotVersion, scope: str) -> bool:
    normalized_scope = scope.strip().lower()
    if not normalized_scope:
        return False
    version_name = str(version.name or "").lower()
    return (
        str(version.id).lower() == normalized_scope
        or version_name == normalized_scope
        or f"v{version.version_no}".lower() == normalized_scope
        or str(version.version_no) == normalized_scope
    )


def _write_audit_log(
    db: Session,
    request: Request,
    current_user: User,
    action_type: str,
    target_type: str,
    target_id: UUID | None,
    before_json: dict[str, object] | None = None,
    after_json: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            before_json=before_json,
            after_json=after_json,
            ip_address=request.client.host if request.client else None,
        )
    )


def _close_open_channel_rooms_for_version_change(
    db: Session,
    request: Request,
    current_user: User,
    bot: Bot,
    previous_version_id: UUID | None,
    next_version_id: UUID | None,
) -> int:
    if previous_version_id is None or previous_version_id == next_version_id:
        return 0
    from app.models import ChannelLlmBinding
    rooms = db.scalars(
        select(ChannelRoom).where(
            ChannelRoom.bot_id == bot.id,
            ChannelRoom.bot_version_id == previous_version_id,
            ChannelRoom.status == "open",
            ChannelRoom.deleted_at.is_(None),
            ChannelRoom.id.not_in(select(ChannelLlmBinding.room_id).where(ChannelLlmBinding.status != "closed")),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for room in rooms:
        metadata_json = dict(room.metadata_json or {})
        if room.client_room_id:
            metadata_json["originalClientRoomId"] = room.client_room_id
            room.client_room_id = f"{room.client_room_id}::closed::{room.id}"
        metadata_json["sessionEndReason"] = "active_version_changed"
        metadata_json["endedAt"] = now.isoformat()
        metadata_json["nextActiveVersionId"] = str(next_version_id) if next_version_id else None
        room.metadata_json = metadata_json
        room.status = "closed"
        room.deleted_at = now
        db.add(room)
    if rooms:
        _write_audit_log(
            db,
            request,
            current_user,
            action_type="channel.session.bulk_close",
            target_type="bot",
            target_id=bot.id,
            after_json={
                "reason": "active_version_changed",
                "closed_room_count": len(rooms),
                "previous_version_id": str(previous_version_id),
                "next_version_id": str(next_version_id) if next_version_id else None,
            },
        )
    return len(rooms)



@router.get("/talk-templates")
def list_talk_templates(
    request: Request,
    channel_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    filters = [
        AdminTemplate.organization_id == current_user.organization_id,
        AdminTemplate.status == "active",
        AdminTemplate.deleted_at.is_(None),
    ]
    if channel_code:
        filters.append(AdminTemplate.channel_code == channel_code.strip().upper())

    templates = db.execute(
        select(AdminTemplate)
        .where(*filters)
        .order_by(AdminTemplate.channel_code.asc(), AdminTemplate.created_at.asc())
    ).scalars().all()
    channels = db.execute(
        select(AdminChannel).where(
            AdminChannel.organization_id == current_user.organization_id,
            AdminChannel.deleted_at.is_(None),
        )
    ).scalars().all()
    channel_names = {channel.code: channel.name for channel in channels}
    items = [
        _serialize_talk_template(
            template,
            channel_names.get(template.channel_code)
            or str((template.data_json or {}).get("channel_name") or template.channel_code),
        )
        for template in templates
    ]
    return success_response(request, {"items": items, "total": len(items)})

@router.get("")
def list_bots(
    request: Request,
    q: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    include_document: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if current_user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 봇에 접근할 수 있습니다.",
        )
    filters = [
        Bot.organization_id == current_user.organization_id,
        Bot.group_id == current_user.group_id,
        Bot.created_by == current_user.id,
        Bot.deleted_at.is_(None),
    ]
    if q:
        normalized_query = q.strip()
        lookup_conditions = [Bot.name.ilike(f"%{normalized_query}%")]
        try:
            lookup_conditions.append(Bot.id == UUID(normalized_query))
        except ValueError:
            pass
        filters.append(or_(*lookup_conditions))
    if status_filter:
        filters.append(Bot.status == status_filter)

    total = db.scalar(select(func.count(Bot.id)).where(*filters)) or 0
    start = (page - 1) * page_size
    page_items = db.scalars(
        select(Bot)
        .where(*filters)
        .order_by(Bot.updated_at.desc())
        .offset(start)
        .limit(page_size)
    ).all()

    group = db.get(Group, current_user.group_id)
    active_version_ids = [bot.active_version_id for bot in page_items if bot.active_version_id is not None]
    active_versions = (
        db.scalars(
            select(BotVersion)
            .where(
                BotVersion.id.in_(active_version_ids),
                BotVersion.deleted_at.is_(None),
            )
            .options(defer(BotVersion.version_json))
        ).all()
        if active_version_ids
        else []
    )
    active_version_by_id = {version.id: version for version in active_versions}
    bot_ids = [bot.id for bot in page_items]
    version_count_by_bot: dict[UUID, int] = {}
    if bot_ids:
        version_count_by_bot = {
            row.bot_id: int(row.count or 0)
            for row in db.execute(
                select(BotVersion.bot_id, func.count(BotVersion.id).label("count"))
                .where(
                    BotVersion.bot_id.in_(bot_ids),
                    BotVersion.deleted_at.is_(None),
                )
                .group_by(BotVersion.bot_id)
            )
        }
    audit_context = _build_version_audit_context(db, active_versions) if not include_document else None

    return success_response(
        request,
        [
            _serialize_bot(db, bot) if include_document else _serialize_bot_summary(
                db,
                bot,
                active_version_override=active_version_by_id.get(bot.active_version_id),
                version_count_override=version_count_by_bot.get(bot.id, 0),
                audit_context=audit_context,
                group_override=group,
            )
            for bot in page_items
        ],
        meta={
            "page": page,
            "page_size": page_size,
            "total": total,
        },
    )


@router.post("")
def create_bot(
    payload: BotCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if current_user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 봇을 생성할 수 있습니다.",
        )
    existing_bot = db.scalar(
        select(Bot.id).where(Bot.created_by == current_user.id, Bot.deleted_at.is_(None)).limit(1)
    )
    if existing_bot is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="사용자당 봇은 하나만 생성할 수 있습니다.")
    assert_license_allows_creation(db, current_user.organization_id, "bots")
    bot_id = uuid4()
    bot = Bot(
        id=bot_id,
        organization_id=current_user.organization_id,
        group_id=current_user.group_id,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        status="active",
        data_json=_build_bot_json(payload),
        created_by=current_user.id,
    )
    db.add(bot)
    try:
        db.flush()
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) != "uq_bots_live_creator":
            raise
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="사용자당 봇은 하나만 생성할 수 있습니다.",
        ) from error
    profile_image_url = _store_profile_image(bot.id, payload.profile_image_data)
    if profile_image_url:
        bot_data = dict(bot.data_json or {})
        bot_data["profile_image_url"] = profile_image_url
        bot.data_json = bot_data
        db.add(bot)
        db.flush()

    if payload.bot_kind == "hub":
        db.add(BotHub(bot_id=bot.id, call_method=payload.hub_call_method))
        db.flush()

    initial_document = build_default_version_document()
    initial_version = BotVersion(
        bot_id=bot.id,
        version_no=1,
        name="v1",
        description=payload.description.strip() if payload.description else None,
        comment="초기 생성 버전",
        status="testing",
        version_json=initial_document,
        created_by=current_user.id,
    )
    _refresh_version_read_snapshot(initial_version, initial_document)
    db.add(initial_version)
    db.flush()

    # 버튼형 허브는 NLU 학습 없이 구성된 운영 봇을 선택하므로 생성 직후 운영 상태로 둡니다.
    if payload.bot_kind == "hub" and payload.hub_call_method == "button":
        initial_version.status = "active"
        initial_version.activated_at = datetime.now(timezone.utc)
        initial_version.activated_by = current_user.id
        bot.active_version_id = initial_version.id
        db.add(initial_version)
        db.add(bot)
        db.flush()

    after_json = _serialize_bot(db, bot)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.create",
        target_type="bot",
        target_id=bot.id,
        after_json=after_json,
    )
    db.commit()
    db.refresh(bot)

    return success_response(request, _serialize_bot(db, bot))


@router.get("/api-catalog")
def list_api_catalog_bots(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if current_user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 봇에 접근할 수 있습니다.",
        )

    bots = db.scalars(
        select(Bot)
        .where(
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == current_user.group_id,
            Bot.created_by == current_user.id,
            Bot.deleted_at.is_(None),
        )
        .order_by(Bot.updated_at.desc())
    ).all()
    active_version_ids = [bot.active_version_id for bot in bots if bot.active_version_id is not None]
    active_versions = (
        db.scalars(
            select(BotVersion).where(
                BotVersion.id.in_(active_version_ids),
                BotVersion.deleted_at.is_(None),
            ).options(defer(BotVersion.version_json))
        ).all()
        if active_version_ids
        else []
    )
    active_version_by_id = {version.id: version for version in active_versions}
    version_counts = (
        dict(
            db.execute(
                select(BotVersion.bot_id, func.count(BotVersion.id))
                .where(
                    BotVersion.bot_id.in_([bot.id for bot in bots]),
                    BotVersion.deleted_at.is_(None),
                )
                .group_by(BotVersion.bot_id)
            ).all()
        )
        if bots
        else {}
    )
    bot_stamps = [
        ":".join(
            [
                str(bot.id),
                _iso(bot.updated_at) or "unknown",
                str(bot.active_version_id or ""),
                _iso(active_version_by_id[bot.active_version_id].updated_at) if bot.active_version_id in active_version_by_id else "none",
                str(version_counts.get(bot.id) or 0),
            ]
        )
        for bot in bots
    ]
    cache_key = (
        f"bot-api-catalog:{current_user.organization_id}:{current_user.group_id}:"
        f"{_cache_stamp_digest('|'.join(bot_stamps))}"
    )

    def _produce_api_catalog_items() -> list[dict[str, object]]:
        audit_context = _build_version_audit_context(db, active_versions)
        return [
            _serialize_bot_api_catalog_summary(
                db,
                bot,
                active_version_override=active_version_by_id.get(bot.active_version_id),
                version_count_override=int(version_counts.get(bot.id) or 0),
                audit_context=audit_context,
            )
            for bot in bots
        ]

    return success_response(
        request,
        cache_aside_json(
            cache_key,
            _produce_api_catalog_items,
            ttl_seconds=settings.cache_default_ttl_seconds,
        ),
        meta={"total": len(bots)},
    )


@router.get("/group-apis")
def list_group_apis(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if current_user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 API에 접근할 수 있습니다.",
        )

    group = db.get(Group, current_user.group_id)
    if group is None or group.organization_id != current_user.organization_id or group.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="그룹을 찾을 수 없습니다.")

    group_apis = _ensure_group_api_assets(db, group, current_user)
    items = []
    for api in group_apis:
        item = deepcopy(api)
        item["usageCount"] = _group_api_reference_count(db, current_user, api)
        items.append(item)

    return success_response(
        request,
        {
            "items": items,
            "total": len(items),
        },
        meta={"total": len(items)},
    )


@router.patch("/group-apis")
def update_group_apis(
    payload: VersionDocumentItemsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_api_write_user(db, current_user)
    if current_user.group_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="그룹이 지정된 사용자만 API를 수정할 수 있습니다.",
        )

    group = db.get(Group, current_user.group_id)
    if group is None or group.organization_id != current_user.organization_id or group.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="그룹을 찾을 수 없습니다.")

    before_json = {"items": _group_api_assets(group)}
    next_items = _dedupe_group_api_asset_list(payload.items)
    _set_group_api_assets(group, next_items)
    db.add(group)
    db.flush()

    after_items = _group_api_assets(group)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="group.apis.update",
        target_type="group",
        target_id=group.id,
        before_json=before_json,
        after_json={"items": after_items},
    )
    db.commit()
    db.refresh(group)
    purge_cache_pattern(f"bot-api-catalog:{current_user.organization_id}:{current_user.group_id}:*")

    items = []
    for api in _group_api_assets(group):
        item = deepcopy(api)
        item["usageCount"] = _group_api_reference_count(db, current_user, api)
        items.append(item)

    return success_response(
        request,
        {
            "items": items,
            "total": len(items),
        },
        meta={"total": len(items)},
    )


@router.get("/{bot_id}/versions/{version_scope}/context")
def get_bot_workspace_context(
    bot_id: UUID,
    version_scope: str,
    request: Request,
    include_document: bool = Query(default=False),
    prefetch_section: str | None = Query(default=None, pattern="^(dialogs)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    versions = _list_bot_versions(db, bot.id, include_document=include_document)
    selected_version = next((version for version in versions if _matches_version_scope(version, version_scope)), None)
    if selected_version is None and bot.active_version_id is not None:
        selected_version = next((version for version in versions if version.id == bot.active_version_id), None)
    if selected_version is None:
        selected_version = versions[0] if versions else None

    def _produce_workspace_context() -> dict[str, object]:
        source_versions = versions
        source_selected_version = next((version for version in source_versions if _matches_version_scope(version, version_scope)), None)
        if source_selected_version is None and bot.active_version_id is not None:
            source_selected_version = next((version for version in source_versions if version.id == bot.active_version_id), None)
        if source_selected_version is None:
            source_selected_version = source_versions[0] if source_versions else None
        audit_context = _build_version_audit_context(db, source_versions)
        selected_version_detail = (
            _serialize_version(bot, source_selected_version, db, audit_context)
            if source_selected_version is not None and include_document
            else _serialize_version_summary(bot, source_selected_version, db, audit_context=audit_context)
            if source_selected_version is not None
            else None
        )
        if (
            source_selected_version is not None
            and not include_document
            and prefetch_section == "dialogs"
            and isinstance(selected_version_detail, dict)
        ):
            dialogs_payload = _serialize_version_dialogs(bot, source_selected_version, db)
            selected_version_detail = {
                **selected_version_detail,
                "version_json": {
                    "dialogs": deepcopy(dialogs_payload.get("items") if isinstance(dialogs_payload, dict) else []),
                },
                "asset_counts": deepcopy(dialogs_payload.get("asset_counts") if isinstance(dialogs_payload, dict) else {}),
                "scenario_validation": deepcopy(dialogs_payload.get("scenario_validation") if isinstance(dialogs_payload, dict) else {}),
            }
        counts = selected_version_detail.get("asset_counts", {}) if isinstance(selected_version_detail, dict) else {}
        scenario_validation = selected_version_detail.get("scenario_validation", {}) if isinstance(selected_version_detail, dict) else {}
        return {
            "bot": _serialize_bot_summary(
                db,
                bot,
                active_version_override=next((version for version in source_versions if version.id == bot.active_version_id), None),
                version_count_override=len(source_versions),
                audit_context=audit_context,
            ),
            "versions": [_serialize_version_summary(bot, version, db, audit_context=audit_context) for version in source_versions],
            "version": selected_version_detail,
            "counts": counts,
            "permissions": {
                "can_edit": True,
                "can_train": True,
                "can_publish": True,
            },
            "health": {
                "has_scenario_error": int(scenario_validation.get("error_count") or 0) > 0 if isinstance(scenario_validation, dict) else False,
                "has_training_error": False,
            },
        }

    if not include_document:
        return success_response(
            request,
            cache_aside_json(
                f"bot-workspace-context-summary:v5:{bot.id}:{version_scope}:{prefetch_section or 'none'}:{_cache_stamp_digest(_version_collection_cache_stamp(bot, versions))}",
                _produce_workspace_context,
                ttl_seconds=settings.cache_default_ttl_seconds,
            ),
        )

    return success_response(request, _produce_workspace_context())


@router.get("/{bot_id}/versions/{version_scope}/settings-context")
def get_bot_settings_context(
    bot_id: UUID,
    version_scope: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    versions = _list_bot_versions(db, bot.id, include_document=False)
    selected_version = next((version for version in versions if _matches_version_scope(version, version_scope)), None)
    if selected_version is None and bot.active_version_id is not None:
        selected_version = next((version for version in versions if version.id == bot.active_version_id), None)
    if selected_version is None:
        selected_version = versions[0] if versions else None

    def _produce_settings_context() -> dict[str, object]:
        source_versions = versions
        source_selected_version = next((version for version in source_versions if _matches_version_scope(version, version_scope)), None)
        if source_selected_version is None and bot.active_version_id is not None:
            source_selected_version = next((version for version in source_versions if version.id == bot.active_version_id), None)
        if source_selected_version is None:
            source_selected_version = source_versions[0] if source_versions else None
        selected_version_detail = (
            _serialize_version_settings_summary(
                bot,
                source_selected_version,
                db,
                include_ai_config=True,
                include_dialogs=True,
            )
            if source_selected_version is not None
            else None
        )
        return {
            "bot": _serialize_bot_settings_summary(
                db,
                bot,
                active_version_override=next((version for version in source_versions if version.id == bot.active_version_id), None),
                version_count_override=len(source_versions),
            ),
            "versions": [_serialize_version_settings_summary(bot, version) for version in source_versions],
            "version": selected_version_detail,
            "counts": {},
            "permissions": {
                "can_edit": True,
                "can_train": True,
                "can_publish": True,
            },
            "health": {
                "has_scenario_error": False,
                "has_training_error": False,
            },
        }

    return success_response(
        request,
        cache_aside_json(
            f"bot-settings-context:{bot.id}:{version_scope}:{_cache_stamp_digest(_version_collection_cache_stamp(bot, versions))}",
            _produce_settings_context,
            ttl_seconds=settings.cache_default_ttl_seconds,
        ),
    )


@router.get("/{bot_id}")
def get_bot(
    bot_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    return success_response(request, _serialize_bot(db, bot))


@router.patch("/{bot_id}")
@version_mutation("bot_settings")
def update_bot(
    bot_id: UUID,
    payload: BotUpdateRequest,
    request: Request,
    response_mode: str = Query(default="full", pattern="^(full|summary|settings)$"),
    response_version_scope: str | None = Query(default=None, min_length=1, max_length=120),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    before_json = _serialize_bot(db, bot)

    updates = payload.model_dump(exclude_unset=True)
    data_json = dict(bot.data_json or {})
    ai_updates = {key: updates[key] for key in AI_CONFIG_KEYS if key in updates}
    ai_target_version: BotVersion | None = None
    settings_scope = updates.get("settings_scope")
    if ai_updates and isinstance(settings_scope, str) and settings_scope.strip():
        ai_target_version = _get_version_by_scope(db, bot.id, settings_scope)
        if ai_target_version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="AI 설정을 저장할 버전을 찾을 수 없습니다.",
            )
        ai_updates = {key: value for key, value in ai_updates.items() if key not in LOCKED_AI_CONFIG_KEYS}

    write_target_version = ai_target_version
    if write_target_version is None and "settings_json" in updates and isinstance(settings_scope, str) and settings_scope.strip():
        write_target_version = _get_version_by_scope(db, bot.id, settings_scope)
    if write_target_version is not None:
        admit_version_write(db, write_target_version)
    elif ai_updates or "settings_json" in updates or "bot_kind" in updates:
        admit_bot_version_writes(db, bot)

    immutable_bot_keys = ("language", "nlu_type", "nlu_engine", "nlu_model", "answer_mode")
    for key in immutable_bot_keys:
        if key not in updates or ai_target_version is not None:
            continue
        current_value = data_json.get(key)
        if key == "nlu_engine" and current_value is None:
            current_value = data_json.get("nlu_model")
        if key == "nlu_model" and current_value is None:
            current_value = data_json.get("nlu_engine")
        if _normalize_engine_value(key, current_value) != _normalize_engine_value(key, updates.get(key)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=LOCKED_AI_CONFIG_MESSAGE,
            )

    lock_target_config = _get_version_ai_config(bot, ai_target_version) if ai_target_version is not None else data_json
    lock_target_trained = _is_version_trained(ai_target_version, db) if ai_target_version is not None else _bot_has_trained_version(db, bot)
    if _engine_update_changes_locked_value(lock_target_config, updates) and lock_target_trained:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=LOCKED_AI_CONFIG_MESSAGE,
        )
    if "name" in updates and updates["name"]:
        bot.name = updates["name"].strip()
    if "description" in updates:
        bot.description = updates["description"].strip() if updates["description"] else None
    if "status" in updates and updates["status"]:
        bot.status = updates["status"].strip()
    if "bot_kind" in updates:
        data_json["bot_kind"] = updates["bot_kind"]
    if "bot_mode" in updates:
        data_json["bot_mode"] = updates["bot_mode"]
    if "profile_key" in updates:
        data_json["profile_key"] = updates["profile_key"]
    if "profile_image_data" in updates:
        profile_image_url = _store_profile_image(bot.id, updates["profile_image_data"])
        if profile_image_url:
            data_json["profile_image_url"] = profile_image_url
        else:
            _remove_profile_images(bot.id)
            data_json.pop("profile_image_url", None)
    if "language" in updates and ai_target_version is None:
        data_json["language"] = updates["language"]
    if "nlu_engine" in updates and ai_target_version is None:
        data_json["nlu_engine"] = updates["nlu_engine"]
    if "nlu_type" in updates and ai_target_version is None:
        data_json["nlu_type"] = updates["nlu_type"]
    if "nlu_model" in updates and ai_target_version is None:
        data_json["nlu_model"] = updates["nlu_model"]
    if "answer_mode" in updates and ai_target_version is None:
        data_json["answer_mode"] = updates["answer_mode"]
    if "llm_provider" in updates and ai_target_version is None:
        data_json["llm_provider"] = updates["llm_provider"]
    if "llm_model" in updates and ai_target_version is None:
        data_json["llm_model"] = updates["llm_model"]
    if "llm_base_url" in updates and ai_target_version is None:
        data_json["llm_base_url"] = updates["llm_base_url"].strip() if updates["llm_base_url"] else None
    if "vector_connections" in updates and ai_target_version is None:
        data_json["vector_connections"] = updates["vector_connections"] or {}
    if "configuration_scoring" in updates and ai_target_version is None:
        data_json["configuration_scoring"] = updates["configuration_scoring"] or {}
    if "introduction" in updates:
        data_json["introduction"] = updates["introduction"].strip() if updates["introduction"] else None
    if updates.get("settings_scope") and isinstance(updates.get("settings_json"), dict):
        settings_scope = updates["settings_scope"].strip()
        settings_version = _get_version_by_scope(db, bot.id, settings_scope)
        if settings_version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="설정을 저장할 버전을 찾을 수 없습니다.",
            )
        settings_json = updates["settings_json"]
        settings_by_version = dict(data_json.get("settings_by_version") or {})
        canonical_scope = str(settings_version.id)
        existing_settings = settings_by_version.get(canonical_scope)
        if not isinstance(existing_settings, dict):
            legacy_settings = settings_by_version.get(settings_version.name)
            existing_settings = legacy_settings if isinstance(legacy_settings, dict) else {}
        settings_json = _apply_smalltalk_item_metadata(
            existing_settings,
            settings_json,
            current_user,
            datetime.now(timezone.utc),
        )
        settings_by_version[canonical_scope] = _merge_json_dict(existing_settings, settings_json)
        data_json["settings_by_version"] = settings_by_version

    answer_vector_before = (
        _answer_vector_config_fingerprint(_get_version_ai_config(bot, ai_target_version))
        if ai_target_version is not None and "vector_connections" in ai_updates
        else None
    )
    answer_embedding_before = (
        _answer_embedding_config_fingerprint(_get_version_ai_config(bot, ai_target_version))
        if ai_target_version is not None and any(key in ai_updates for key in ("nlu_model", "nlu_engine", "llm_provider", "llm_model"))
        else None
    )
    nlu_model_before = (
        _normalize_engine_value("nlu_model", _get_version_ai_config(bot, ai_target_version).get("nlu_model"))
        if ai_target_version is not None and any(key in ai_updates for key in ("nlu_model", "nlu_engine"))
        else None
    )
    if ai_target_version is not None and ai_updates:
        _set_version_ai_config(ai_target_version, ai_updates)
        nlu_model_after = (
            _normalize_engine_value("nlu_model", _get_version_ai_config(bot, ai_target_version).get("nlu_model"))
            if nlu_model_before is not None
            else None
        )
        if nlu_model_before is not None and nlu_model_before != nlu_model_after:
            next_version_json = _mark_nlu_training_retrain_required(
                ai_target_version.version_json,
                "NLU 모델이 변경되어 다시 학습해야 합니다.",
            )
            _assign_version_document(ai_target_version, next_version_json)
        answer_vector_after = (
            _answer_vector_config_fingerprint(_get_version_ai_config(bot, ai_target_version))
            if answer_vector_before is not None
            else None
        )
        if answer_vector_before is not None and answer_vector_before != answer_vector_after:
            next_version_json = _mark_answer_training_reembed_required(
                ai_target_version.version_json,
                "Answer Vector DB 설정이 변경되어 답변 문서 재임베딩이 필요합니다.",
            )
            _assign_version_document(ai_target_version, next_version_json)
        answer_embedding_after = (
            _answer_embedding_config_fingerprint(_get_version_ai_config(bot, ai_target_version))
            if answer_embedding_before is not None
            else None
        )
        if answer_embedding_before is not None and answer_embedding_before != answer_embedding_after:
            next_version_json = _mark_answer_training_reembed_required(
                ai_target_version.version_json,
                "답변 임베딩 모델이 변경되어 답변 문서 재임베딩이 필요합니다.",
            )
            _assign_version_document(ai_target_version, next_version_json)
        ai_target_version.updated_at = datetime.now(timezone.utc)
        db.add(ai_target_version)

    bot.data_json = data_json

    db.add(bot)
    db.flush()
    after_json = _serialize_bot(db, bot)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.update",
        target_type="bot",
        target_id=bot.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(bot)

    if response_mode == "summary":
        return success_response(request, _serialize_bot_summary(db, bot))
    if response_mode == "settings":
        response_version = _get_version_by_scope(db, bot.id, response_version_scope) if response_version_scope else None
        return success_response(
            request,
            _serialize_bot_settings_summary(db, bot, active_version_override=response_version),
        )

    return success_response(request, _serialize_bot(db, bot))


@router.delete("/{bot_id}")
@version_mutation("bot_delete")
def delete_bot(
    bot_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    admit_bot_version_writes(db, bot)
    active_version = None
    if bot.active_version_id:
        active_version = db.scalar(
            select(BotVersion).where(
                BotVersion.id == bot.active_version_id,
                BotVersion.deleted_at.is_(None),
            )
        )
    if active_version and active_version.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="운영 버전이 있는 봇은 삭제할 수 없습니다. 먼저 운영을 해제해주세요.",
        )
    before_json = _serialize_bot(db, bot)
    deleted_at = datetime.now(timezone.utc)

    bot.deleted_at = deleted_at
    bot.active_version_id = None
    db.add(bot)
    versions = db.scalars(
        select(BotVersion).where(
            BotVersion.bot_id == bot.id,
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    for version in versions:
        version.deleted_at = deleted_at
        db.add(version)

    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.delete",
        target_type="bot",
        target_id=bot.id,
        before_json=before_json,
        after_json={
            "id": str(bot.id),
            "deleted_at": _iso(bot.deleted_at),
        },
    )
    db.commit()
    for version in versions:
        _purge_version_cache(version)

    return success_response(
        request,
        {
            "message": "봇이 삭제되었습니다.",
        },
    )


@router.get("/{bot_id}/versions")
def list_versions(
    bot_id: UUID,
    request: Request,
    include_document: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    versions = _list_bot_versions(db, bot.id, include_document=include_document)
    if include_document:
        audit_context = _build_version_audit_context(db, versions)
        return success_response(request, [_serialize_version(bot, version, db, audit_context) for version in versions])

    def _produce_version_summaries() -> list[dict[str, object]]:
        audit_context = _build_version_audit_context(db, versions)
        return [_serialize_version_summary(bot, version, db, audit_context=audit_context) for version in versions]

    return success_response(
        request,
        cache_aside_json(
            f"bot-versions-summary:{bot.id}:{_cache_stamp_digest(_version_collection_cache_stamp(bot, versions))}",
            _produce_version_summaries,
            ttl_seconds=settings.cache_default_ttl_seconds,
        ),
    )


@router.post("/{bot_id}/versions")
def create_version(
    bot_id: UUID,
    payload: VersionCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    next_version_no = _next_bot_version_no(db, bot.id)

    version_document = build_default_version_document()
    version = BotVersion(
        bot_id=bot.id,
        version_no=next_version_no,
        name=(payload.name.strip() if payload.name else f"v{next_version_no}"),
        description=payload.description.strip() if payload.description else None,
        comment=payload.comment.strip() if payload.comment else None,
        status="draft",
        version_json=version_document,
        created_by=current_user.id,
    )
    _refresh_version_read_snapshot(version, version_document)
    db.add(version)
    db.flush()
    if str(bot.id) == settings.persistent_llm_sample_bot_id:
        register_sample_version(db, version.id)

    after_json = _serialize_version(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.create",
        target_type="bot_version",
        target_id=version.id,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)

    return success_response(request, _serialize_version(bot, version, db))


@router.get("/{bot_id}/versions/{version_id}")
def get_version(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(request, _serialize_version(bot, version, db))


@router.get("/{bot_id}/versions/{version_id}/dialogs")
def get_version_dialogs(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "dialogs", lambda: _serialize_version_dialogs(bot, version, db)),
    )


@router.patch("/{bot_id}/versions/{version_id}/dialogs")
@version_mutation("edit")
def update_version_dialogs(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionDocumentItemsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    _assert_dialog_updates_not_locked_by_other(db, version, payload.items, current_user)
    before_json = _serialize_version_dialogs(bot, version, db)
    next_version_json = _version_document_with_dialogs(version, payload.items, db)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    sync_version_dialog_split_tables(
        db,
        bot,
        version,
        next_version_json,
        include_dialogs=True,
        include_graphs=False,
    )
    db.flush()
    after_json = _serialize_version_dialogs(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.dialogs.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_dialogs(bot, version, db))


@router.get("/{bot_id}/versions/{version_id}/dialogs/{dialog_id}/flow")
def get_version_dialog_flow(
    bot_id: UUID,
    version_id: UUID,
    dialog_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(
            version,
            "dialog_flow",
            lambda: _serialize_version_dialog_flow(bot, version, dialog_id, db),
            extra=dialog_id,
        ),
    )


@router.patch("/{bot_id}/versions/{version_id}/dialogs/{dialog_id}/flow")
@version_mutation("edit")
def update_version_dialog_flow(
    bot_id: UUID,
    version_id: UUID,
    dialog_id: str,
    payload: VersionDialogFlowUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    _assert_dialog_not_locked_by_other(db, version, dialog_id, current_user)
    before_json = _serialize_version_dialog_flow(bot, version, dialog_id, db)
    next_version_json = _version_document_with_dialog_flow(version, dialog_id, payload.dialog, payload.graph)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    sync_version_dialog_split_tables(
        db,
        bot,
        version,
        next_version_json,
        include_dialogs=True,
        include_graphs=True,
    )
    db.flush()
    after_json = _serialize_version_dialog_flow(bot, version, dialog_id, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.dialog_flow.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_dialog_flow(bot, version, dialog_id, db))


@router.get("/{bot_id}/versions/{version_id}/entities")
def get_version_entities(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "entities", lambda: _serialize_version_document_items(bot, version, "entities", db)),
    )


@router.patch("/{bot_id}/versions/{version_id}/entities")
@version_mutation("edit")
def update_version_entities(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionDocumentItemsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version_document_items(bot, version, "entities", db)
    next_version_json = _version_document_with_items(version, "entities", payload.items)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    db.flush()
    after_json = _serialize_version_document_items(bot, version, "entities", db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.entities.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_document_items(bot, version, "entities", db))


@router.get("/{bot_id}/versions/{version_id}/dictionary")
def get_version_dictionary(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "dictionary", lambda: _serialize_version_document_items(bot, version, "dictionary", db)),
    )


@router.patch("/{bot_id}/versions/{version_id}/dictionary")
@version_mutation("edit")
def update_version_dictionary(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionDocumentItemsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version_document_items(bot, version, "dictionary", db)
    next_version_json = _version_document_with_items(version, "dictionary", payload.items)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    db.flush()
    after_json = _serialize_version_document_items(bot, version, "dictionary", db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.dictionary.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_document_items(bot, version, "dictionary", db))


@router.get("/{bot_id}/versions/{version_id}/apis")
def get_version_apis(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "apis", lambda: _serialize_version_document_items(bot, version, "apis", db)),
    )


@router.patch("/{bot_id}/versions/{version_id}/apis")
@version_mutation("edit")
def update_version_apis(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionDocumentItemsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_api_write_user(db, current_user)
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version_document_items(bot, version, "apis", db)
    next_version_json = _version_document_with_items(version, "apis", payload.items)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )
    current_api_count = count_registered_apis(db, current_user.organization_id)
    next_api_count = count_registered_apis_with_version_override(
        db,
        current_user.organization_id,
        version.id,
        next_version_json,
    )
    api_delta = next_api_count - current_api_count
    if api_delta > 0:
        assert_license_allows_creation(db, current_user.organization_id, "apis", api_delta)

    _assign_version_document(version, next_version_json)
    db.add(version)
    db.flush()
    after_json = _serialize_version_document_items(bot, version, "apis", db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.apis.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_document_items(bot, version, "apis", db))


@router.get("/{bot_id}/versions/{version_id}/references")
def get_version_references(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "references", lambda: _serialize_version_reference_items(bot, version, db)),
    )


@router.get("/{bot_id}/versions/{version_id}/configure")
def get_version_configure(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    include_dialog_flow_graphs: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(
            version,
            "configure",
            lambda: _serialize_version_configure(
                bot,
                version,
                db,
                include_dialog_flow_graphs=include_dialog_flow_graphs,
            ),
            extra="with-flow" if include_dialog_flow_graphs else "no-flow",
        ),
    )


@router.patch("/{bot_id}/versions/{version_id}/configure")
@version_mutation("edit")
def update_version_configure(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionConfigureUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version_configure(bot, version, db)
    next_version_json = _version_document_with_configure(version, payload.dialogs, payload.dialog_flow_graphs)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    sync_version_dialog_split_tables(
        db,
        bot,
        version,
        next_version_json,
        include_dialogs=True,
        include_graphs=True,
    )
    db.flush()
    after_json = _serialize_version_configure(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.configure.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_configure(bot, version, db))


@router.get("/{bot_id}/versions/{version_id}/retraining")
def get_version_retraining(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(
        request,
        _cached_version_payload(version, "retraining", lambda: _serialize_version_retraining(bot, version, db)),
    )


@router.patch("/{bot_id}/versions/{version_id}/retraining")
@version_mutation("edit")
def update_version_retraining(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionRetrainingUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version_retraining(bot, version, db)
    next_version_json = _version_document_with_retraining(version, payload.dialogs, payload.system_config)
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    _assign_version_document(version, next_version_json)
    db.add(version)
    sync_version_dialog_split_tables(
        db,
        bot,
        version,
        next_version_json,
        include_dialogs=True,
        include_graphs=False,
    )
    db.flush()
    after_json = _serialize_version_retraining(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.retraining.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version_retraining(bot, version, db))


@router.patch("/{bot_id}/versions/{version_id}")
@version_mutation("edit")
def update_version(
    bot_id: UUID,
    version_id: UUID,
    payload: VersionUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version(bot, version, db)

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"]:
        version.name = updates["name"].strip()
    if "description" in updates:
        version.description = updates["description"].strip() if updates["description"] else None
    if "comment" in updates:
        version.comment = updates["comment"].strip() if updates["comment"] else None
    next_version_json_for_split: dict[str, object] | None = None
    if "status" in updates and updates["status"]:
        version.status = updates["status"].strip()
    if "version_json" in updates and isinstance(updates["version_json"], dict):
        next_version_json = _prepare_version_document_for_save(updates["version_json"], _get_nlu_training_state(version))
        scenario_validation = scenario_validation_from_version(next_version_json)
        if save_blocking_scenario_items(scenario_validation):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "저장할 수 없는 대화 설계 오류가 있습니다. 연결 대상과 Function 출력 Parameter를 확인해주세요.",
                    "scenario_validation": scenario_validation,
                },
            )
        current_api_count = count_registered_apis(db, current_user.organization_id)
        next_api_count = count_registered_apis_with_version_override(
            db,
            current_user.organization_id,
            version.id,
            next_version_json,
        )
        api_delta = next_api_count - current_api_count
        if api_delta > 0:
            assert_license_allows_creation(db, current_user.organization_id, "apis", api_delta)
        _assert_full_version_update_preserves_loaded_sections(version.version_json, next_version_json)
        _assign_version_document(version, next_version_json)
        next_version_json_for_split = next_version_json

    db.add(version)
    if next_version_json_for_split is not None:
        sync_version_dialog_split_tables(
            db,
            bot,
            version,
            next_version_json_for_split,
            include_dialogs=True,
            include_graphs=True,
        )
    db.flush()
    after_json = _serialize_version(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.update",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)

    return success_response(request, _serialize_version(bot, version, db))


@router.post("/{bot_id}/versions/{version_id}/answers/rag/embed")
@version_mutation("rag_write")
def embed_version_rag_answers(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    payload: VersionNluTrainRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    if not _is_rag_answer_mode(ai_config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="답변 방식이 RAG인 Semantic 또는 LLM NLU 봇에서만 답변 문서를 임베딩할 수 있습니다.",
        )
    answer_training_documents = _build_answer_training_documents(version.version_json, payload, ai_config)
    answer_training_result = _train_rag_answer_vector_index(bot, version, ai_config, answer_training_documents)
    if answer_training_result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변 임베딩에 사용할 문서가 없습니다.")
    _save_answer_training_result(
        db=db,
        request=request,
        current_user=current_user,
        bot=bot,
        version=version,
        answer_training_result=answer_training_result,
    )
    return success_response(request, answer_training_result)


@router.post("/{bot_id}/versions/{version_id}/answers/rag/embed-pdf")
@version_mutation("rag_write")
async def embed_version_rag_answer_pdf(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    title: str | None = Form(default=None),
    embedding_provider: str | None = Form(default=None),
    embedding_model: str | None = Form(default=None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    if not _is_rag_answer_mode(ai_config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="답변 방식이 RAG인 Semantic 또는 LLM NLU 봇에서만 답변 문서를 임베딩할 수 있습니다.",
        )
    if file.content_type and file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 파일만 업로드할 수 있습니다.")

    source_text = _extract_pdf_text_from_bytes(await file.read())
    payload = VersionNluTrainRequest(
        answer_training={
            "source_type": "pdf",
            "title": title,
            "text": source_text,
            "file_name": file.filename or "",
            "mime_type": file.content_type or "application/pdf",
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
        }
    )
    answer_training_documents = _build_answer_training_documents(version.version_json, payload, ai_config)
    answer_training_result = _train_rag_answer_vector_index(bot, version, ai_config, answer_training_documents)
    if answer_training_result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변 임베딩에 사용할 문서가 없습니다.")
    _save_answer_training_result(
        db=db,
        request=request,
        current_user=current_user,
        bot=bot,
        version=version,
        answer_training_result=answer_training_result,
    )
    return success_response(request, answer_training_result)


@router.post("/{bot_id}/versions/{version_id}/answers/rag/configure")
@version_mutation("rag_write")
def configure_version_rag_answers(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    payload: RagAnswerConfigureRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    if not _is_rag_answer_mode(ai_config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="답변 방식이 RAG인 Semantic 또는 LLM NLU 봇에서만 답변 문서를 구성할 수 있습니다.",
        )
    documents = _build_answer_training_documents(
        version.version_json,
        VersionNluTrainRequest(answer_training=payload.answer_training),
        ai_config,
    )
    result = _configure_rag_answer_documents(
        db=db,
        request=request,
        current_user=current_user,
        bot=bot,
        version=version,
        ai_config=ai_config,
        documents=documents,
        target_count=payload.target_count,
        target_count_policy=payload.target_count_policy,
    )
    return success_response(request, result)


@router.post("/{bot_id}/versions/{version_id}/answers/rag/configure-pdf")
@version_mutation("rag_write")
async def configure_version_rag_answer_pdf(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    title: str | None = Form(default=None),
    embedding_provider: str | None = Form(default=None),
    embedding_model: str | None = Form(default=None),
    target_count: int = Form(default=50),
    target_count_policy: str = Form(default="near"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    if not _is_rag_answer_mode(ai_config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="답변 방식이 RAG인 Semantic 또는 LLM NLU 봇에서만 답변 문서를 구성할 수 있습니다.",
        )
    if file.content_type and file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF 파일만 업로드할 수 있습니다.")
    source_text = _extract_pdf_text_from_bytes(await file.read())
    payload = VersionNluTrainRequest(
        answer_training={
            "source_type": "pdf",
            "title": title,
            "text": source_text,
            "file_name": file.filename or "",
            "mime_type": file.content_type or "application/pdf",
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
        }
    )
    documents = _build_answer_training_documents(version.version_json, payload, ai_config)
    result = _configure_rag_answer_documents(
        db=db,
        request=request,
        current_user=current_user,
        bot=bot,
        version=version,
        ai_config=ai_config,
        documents=documents,
        target_count=max(1, min(int(target_count or 50), 100)),
        target_count_policy=target_count_policy if target_count_policy in {"minimize", "near", "exact"} else "near",
    )
    return success_response(request, result)


def _prepare_natural_hub_training_document(db: Session, bot: Bot, version: BotVersion) -> None:
    """Build the hub routing NLU document from its active child bot utterances."""
    bot_data = bot.data_json if isinstance(bot.data_json, dict) else {}
    if str(bot_data.get("bot_kind") or "").strip().lower() != "hub":
        return

    hub = db.get(BotHub, bot.id)
    if hub is None or str(hub.call_method or "button").strip().lower() != "natural":
        return

    rows = db.execute(
        select(BotHubMember, Bot, BotVersion)
        .join(Bot, Bot.id == BotHubMember.bot_id)
        .join(BotVersion, BotVersion.id == Bot.active_version_id)
        .where(
            BotHubMember.hub_id == bot.id,
            Bot.deleted_at.is_(None),
            BotVersion.deleted_at.is_(None),
            BotVersion.status == "active",
        )
        .order_by(BotHubMember.sort_order, BotHubMember.created_at)
    ).all()
    if len(rows) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="자연어형 봇 허브는 학습에 성공한 운영 버전의 하위 봇을 두 개 이상 구성해야 합니다.",
        )

    trained_rows = [
        (member, member_bot, member_version)
        for member, member_bot, member_version in rows
        if _is_version_trained(member_version, db)
    ]
    if len(trained_rows) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="자연어형 봇 허브는 학습에 성공한 운영 버전의 하위 봇을 두 개 이상 구성해야 합니다.",
        )

    dialogs: list[dict[str, object]] = []
    routes: list[dict[str, object]] = []
    for member, member_bot, member_version in trained_rows:
        utterances: list[str] = []
        member_document = normalize_version_document(member_version.version_json)
        for dialog in member_document.get("dialogs", []):
            if isinstance(dialog, dict) and _is_intent_dialog_document(dialog):
                utterances.extend(_extract_training_utterances(dialog))
        unique_utterances = list(dict.fromkeys(item for item in utterances if item))
        if not unique_utterances:
            continue

        dialog_id = f"hub-member-{member_bot.id}"
        dialog_name = str(member.display_name or member_bot.name).strip() or member_bot.name
        dialogs.append({"id": dialog_id, "name": dialog_name, "displayName": dialog_name, "dialogType": "1", "utterances": [{"text": item, "utteranceType": "T"} for item in unique_utterances]})
        routes.append({"dialogId": dialog_id, "botId": str(member_bot.id), "botVersionId": str(member_version.id), "displayName": dialog_name})

    if len(dialogs) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="자연어형 봇 허브의 하위 봇에서 학습문장을 찾지 못했습니다. 각 하위 봇의 운영 버전에 T 학습문장을 등록하고 학습해주세요.",
        )

    document = normalize_version_document(version.version_json)
    document["dialogs"] = dialogs
    system_config = document.get("system_config") if isinstance(document.get("system_config"), dict) else {}
    document["system_config"] = {**system_config, "hub_routing": {"members": routes}}
    _assign_version_document(version, document)
    sync_version_dialog_split_tables(db, bot, version, document, include_dialogs=True, include_graphs=False)
    db.add(version)
    db.flush()


@version_mutation("training")
def _run_version_nlu_training(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    payload: VersionNluTrainRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    _prepare_natural_hub_training_document(db, bot, version)
    ai_config = _get_version_ai_config(bot, version)
    nlu_block_reason = training_block_reason(ai_config)
    if nlu_block_reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=nlu_block_reason)
    scenario_validation = scenario_validation_from_version(version.version_json)
    if int(scenario_validation.get("error_count") or 0) > 0:
        detail = scenario_validation_error_detail(scenario_validation)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"대화 설계 오류가 있는 버전은 학습할 수 없습니다. {detail} 오류를 수정한 뒤 다시 학습해주세요.",
                "scenario_validation": scenario_validation,
            },
        )
    answer_training_result = None
    if payload is not None and payload.answer_training is not None:
        answer_training_documents = _build_answer_training_documents(version.version_json, payload, ai_config)
        answer_training_result = _train_rag_answer_vector_index(bot, version, ai_config, answer_training_documents)
    elif _is_rag_answer_mode(ai_config) and not _has_successful_answer_embedding(version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="RAG 답변 방식에서는 먼저 답변 문서를 임베딩해주세요.",
        )
    training_summary = _get_trainable_intent_summary(version.version_json)
    data_json = ai_config
    nlu_engine_snapshot = _nlu_engine_training_snapshot(ai_config)
    if str(data_json.get("nlu_type") or "ml") == "llm":
        vector_documents = _build_intent_vector_documents(version.version_json)
        if not vector_documents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="LLM NLU 학습 스냅샷에 저장할 T 학습문장이 없습니다. 의도에 T 학습문장을 1개 이상 등록해주세요.",
            )
        ml_settings = _get_ml_settings(bot, version)
        score_cutoff = _float_setting(ml_settings.get("cutOffScore"), 0.75)
        similar_intent_score = _float_setting(ml_settings.get("similarIntentScore"), 0.85)
        max_intent_results = max(1, _int_setting(ml_settings.get("maxIntentResults"), 3))
        training_started_at = datetime.now(timezone.utc)
        trained_at = training_started_at.isoformat()
        training_snapshot = build_llm_nlu_training_snapshot(
            version_json=version.version_json,
            ai_config=ai_config,
            trained_at=trained_at,
            trained_by_login_id=current_user.login_id,
            score_cutoff=score_cutoff,
            similar_intent_score=similar_intent_score,
            max_intent_results=max_intent_results,
        )
        version_json = normalize_version_document(version.version_json)
        system_config = dict(version_json.get("system_config") or {})
        system_config["nlu_training"] = training_snapshot
        system_config["nlu_evaluation"] = {
            "latest": {
                "engine_type": "llm",
                "nlu_type": "llm",
                "nlu_model": training_snapshot.get("nlu_model"),
                "provider": training_snapshot.get("provider"),
                "model": training_snapshot.get("model"),
                "trained_at": trained_at,
                "intent_count": training_snapshot.get("intent_count"),
                "utterance_count": training_snapshot.get("utterance_count"),
                "message": "LLM NLU는 학습 시점에 평가 모델을 생성하지 않고 의도/학습문장 스냅샷을 확정합니다.",
            },
            "history": [],
            "snapshot": training_snapshot.get("snapshot"),
            "quality_diagnostics": {},
        }
        if answer_training_result:
            system_config["answer_training"] = answer_training_result
        version_json["system_config"] = system_config
        _assign_version_document(version, version_json)
        training_completed_at = datetime.now(timezone.utc)
        version.updated_at = training_completed_at
        bot.updated_at = training_completed_at
        db.add(bot)
        db.add(version)
        history_completed_at = training_completed_at
        if training_completed_at.replace(microsecond=0) <= training_started_at.replace(microsecond=0):
            history_completed_at = training_started_at.replace(microsecond=0) + timedelta(seconds=1)
        model_snapshot = {
            "schema_version": training_snapshot.get("schema_version"),
            "status": "success",
            "provider": training_snapshot.get("provider"),
            "model": training_snapshot.get("model"),
            "counts": training_snapshot.get("counts"),
            "intent_count": training_snapshot.get("intent_count"),
            "utterance_count": training_snapshot.get("utterance_count"),
            "started_at": training_started_at.isoformat(),
            "completed_at": history_completed_at.isoformat(),
            "elapsed_ms": max(0, round((training_completed_at - training_started_at).total_seconds() * 1000)),
            **nlu_engine_snapshot,
        }
        if answer_training_result:
            model_snapshot["answer_training"] = answer_training_result
        _write_audit_log(
            db,
            request,
            current_user,
            action_type="bot.version.nlu.train",
            target_type="bot_version",
            target_id=version.id,
            after_json=model_snapshot,
        )
        db.commit()
        _purge_version_cache(version)
        return success_response(
            request,
            _public_hub_payload(bot, {
                "schema_version": training_snapshot.get("schema_version"),
                "engine_type": "llm",
                "model_path": None,
                "model": {
                    "provider": training_snapshot.get("provider"),
                    "model": training_snapshot.get("model"),
                    "trained_at": trained_at,
                    "counts": training_snapshot.get("counts"),
                },
                "counts": {
                    "intent_count": training_snapshot.get("intent_count"),
                    "utterance_count": training_snapshot.get("utterance_count"),
                },
                "evaluation": system_config["nlu_evaluation"]["latest"],
                "answer_training": answer_training_result,
            }),
        )
    if str(data_json.get("nlu_type") or "ml") in SEMANTIC_NLU_TYPES:
        training_started_at = datetime.now(timezone.utc)
        result = _train_semantic_intent_vector_index(bot, version, ai_config)
        semantic_config = intent_vector_config(ai_config)
        vector_documents = _build_intent_vector_documents(version.version_json)
        semantic_evaluation = _calculate_semantic_vector_evaluation(
            bot=bot,
            version=version,
            config=semantic_config,
            trained_at=str(result.get("model", {}).get("trained_at") or ""),
            top_k=3,
        )
        semantic_evaluation.update(nlu_engine_snapshot)
        evaluation_snapshot = semantic_evaluation.pop("snapshot", {})
        split_quality_diagnostics = semantic_evaluation.pop("quality_diagnostics", {})
        training_quality_diagnostics = _build_semantic_training_quality_diagnostics(
            bot=bot,
            version=version,
            config=semantic_config,
            vector_documents=vector_documents,
            top_k=3,
        )
        semantic_evaluation["split_quality_summary"] = split_quality_diagnostics.get("summary", {})
        version_json = normalize_version_document(version.version_json)
        system_config = dict(version_json.get("system_config") or {})
        evaluation_store = dict(system_config.get("nlu_evaluation") or {})
        history = evaluation_store.get("history")
        if not isinstance(history, list):
            history = []
        history = [semantic_evaluation, *history][:20]
        system_config["nlu_evaluation"] = {
            "latest": semantic_evaluation,
            "history": history,
            "snapshot": evaluation_snapshot,
            "quality_diagnostics": split_quality_diagnostics,
            "training_quality_diagnostics": training_quality_diagnostics,
        }
        system_config["nlu_training"] = {
            "status": "success",
            "trained_at": str(result.get("model", {}).get("trained_at") or ""),
            "trained_by_login_id": current_user.login_id,
            "intent_count": result["counts"]["intent_count"],
            "utterance_count": result["counts"]["utterance_count"],
            "model_path": None,
            "vector_index": result.get("model", {}),
            **nlu_engine_snapshot,
        }
        if answer_training_result:
            system_config["answer_training"] = answer_training_result
            result["answer_training"] = answer_training_result
        version_json["system_config"] = system_config
        _assign_version_document(version, version_json)
        training_completed_at = datetime.now(timezone.utc)
        version.updated_at = training_completed_at
        bot.updated_at = training_completed_at
        db.add(bot)
        db.add(version)
        history_completed_at = training_completed_at
        if training_completed_at.replace(microsecond=0) <= training_started_at.replace(microsecond=0):
            history_completed_at = training_started_at.replace(microsecond=0) + timedelta(seconds=1)
        model_snapshot = dict(result["model"])
        model_snapshot["status"] = "success"
        model_snapshot.update(nlu_engine_snapshot)
        model_snapshot["quality_diagnostics"] = split_quality_diagnostics
        model_snapshot["training_quality_diagnostics"] = training_quality_diagnostics
        model_snapshot["started_at"] = training_started_at.isoformat()
        model_snapshot["completed_at"] = history_completed_at.isoformat()
        model_snapshot["elapsed_ms"] = max(0, round((training_completed_at - training_started_at).total_seconds() * 1000))
        if answer_training_result:
            model_snapshot["answer_training"] = answer_training_result
        _write_audit_log(
            db,
            request,
            current_user,
            action_type="bot.version.nlu.train",
            target_type="bot_version",
            target_id=version.id,
            after_json=model_snapshot,
        )
        db.commit()
        _purge_version_cache(version)
        result["evaluation"] = {
            **semantic_evaluation,
            "snapshot": evaluation_snapshot,
            "quality_diagnostics": split_quality_diagnostics,
            "training_quality_diagnostics": training_quality_diagnostics,
        }
        return success_response(request, _public_hub_payload(bot, result))

    if int(training_summary["intent_count"]) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="학습 가능한 의도가 최소 2개 이상이어야 학습할 수 있습니다. 각 의도에는 T 학습문장이 1개 이상 필요합니다.",
        )

    training_started_at = datetime.now(timezone.utc)
    try:
        validation_settings = _get_validation_settings(bot, version)
        version_settings = _get_version_settings(bot, version)
        mark_external_version_write(version.id)
        result = train_and_save_deep_learning_lite_model(
            version,
            imbalance_oversampling=validation_settings.get("imbalanceOversampling") is True,
            version_settings=version_settings,
            language=_bot_language(bot),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    ml_settings = _get_ml_settings(bot, version)
    score_cutoff = _float_setting(ml_settings.get("cutOffScore"), 0.75)
    similar_intent_score = _float_setting(ml_settings.get("similarIntentScore"), 0.85)
    max_intent_results = max(1, _int_setting(ml_settings.get("maxIntentResults"), 3))
    evaluation = calculate_nlu_evaluation(
        version,
        str(result.get("model", {}).get("trained_at") or ""),
        score_cutoff=score_cutoff,
        similar_intent_score=similar_intent_score,
        max_intent_results=max_intent_results,
        version_settings=_get_version_settings(bot, version),
        language=_bot_language(bot),
    )
    evaluation.update(nlu_engine_snapshot)
    evaluation_snapshot = evaluation.pop("snapshot", {})
    quality_diagnostics = evaluation.pop("quality_diagnostics", {})
    version_json = normalize_version_document(version.version_json)
    system_config = dict(version_json.get("system_config") or {})
    evaluation_store = dict(system_config.get("nlu_evaluation") or {})
    history = evaluation_store.get("history")
    if not isinstance(history, list):
        history = []
    history = [evaluation, *history][:20]
    system_config["nlu_evaluation"] = {
        "latest": evaluation,
        "history": history,
        "snapshot": evaluation_snapshot,
        "quality_diagnostics": quality_diagnostics,
    }
    system_config["nlu_training"] = {
        "status": "success",
        "trained_at": str(result.get("model", {}).get("trained_at") or ""),
        "trained_by_login_id": current_user.login_id,
        "intent_count": training_summary["intent_count"],
        "utterance_count": training_summary["utterance_count"],
        "model_path": result.get("model_path"),
        **nlu_engine_snapshot,
    }
    if answer_training_result:
        system_config["answer_training"] = answer_training_result
        result["answer_training"] = answer_training_result
    version_json["system_config"] = system_config
    _assign_version_document(version, version_json)
    result["evaluation"] = {**evaluation, "snapshot": evaluation_snapshot, "quality_diagnostics": quality_diagnostics}
    training_completed_at = datetime.now(timezone.utc)
    version.updated_at = training_completed_at
    bot.updated_at = training_completed_at
    db.add(bot)
    db.add(version)
    history_completed_at = training_completed_at
    if training_completed_at.replace(microsecond=0) <= training_started_at.replace(microsecond=0):
        history_completed_at = training_started_at.replace(microsecond=0) + timedelta(seconds=1)
    model_snapshot = dict(result["model"])
    model_snapshot.update(nlu_engine_snapshot)
    model_snapshot["started_at"] = training_started_at.isoformat()
    model_snapshot["completed_at"] = history_completed_at.isoformat()
    model_snapshot["elapsed_ms"] = max(0, round((training_completed_at - training_started_at).total_seconds() * 1000))
    model_snapshot["quality_diagnostics"] = quality_diagnostics
    if answer_training_result:
        model_snapshot["answer_training"] = answer_training_result

    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.version.nlu.train",
        target_type="bot_version",
        target_id=version.id,
        after_json=model_snapshot,
    )
    db.commit()
    _purge_version_cache(version)

    return success_response(request, _public_hub_payload(bot, result))


def _require_nlu_training_version_exists(db: Session, bot_id: UUID, version_id: UUID) -> None:
    existing_version_id = db.scalar(
        select(BotVersion.id).where(
            BotVersion.id == version_id,
            BotVersion.bot_id == bot_id,
            BotVersion.deleted_at.is_(None),
        )
    )
    if existing_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="버전을 찾을 수 없습니다.",
        )


def _serialize_nlu_training_job(queue_event: ChannelQueueEvent) -> dict[str, object]:
    result_json = dict(queue_event.result_json or {})
    manifest = result_json.get("manifest")
    return {
        "job_id": str(queue_event.id),
        "operation": NLU_TRAINING_QUEUE_OPERATION,
        "status": queue_event.status,
        "receive_status": queue_event.receive_status,
        "requested_at": queue_event.created_at.isoformat() if queue_event.created_at else None,
        "status_changed_at": queue_event.status_changed_at.isoformat() if queue_event.status_changed_at else None,
        "error_message": queue_event.error_message,
        "manifest": manifest if isinstance(manifest, dict) else None,
    }


def _compact_nlu_training_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict):
        return {}
    compact = {
        key: value
        for key, value in manifest.items()
        if key in {"schema_version", "engine_type", "model_path", "model", "counts", "answer_training"}
    }
    evaluation = manifest.get("evaluation")
    if isinstance(evaluation, dict):
        compact["evaluation"] = {
            key: value
            for key, value in evaluation.items()
            if not isinstance(value, (dict, list))
        }
    return compact


def _set_nlu_training_queue_status(
    queue_event: ChannelQueueEvent,
    queue_status: str,
    *,
    receive_status: str | None = None,
    result_json: dict[str, object] | None = None,
    error_message: str | None = None,
) -> None:
    queue_event.status = queue_status
    queue_event.receive_status = receive_status or queue_status
    queue_event.status_changed_at = datetime.now(timezone.utc)
    queue_event.error_message = error_message
    if result_json is not None:
        queue_event.result_json = result_json


@router.post(
    "/{bot_id}/versions/{version_id}/nlu/train",
    status_code=status.HTTP_202_ACCEPTED,
)
@version_mutation("training_queue")
def enqueue_version_nlu_training(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    payload: VersionNluTrainRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not settings.nlu_training_worker_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NLU 학습 Queue 작업기가 비활성화되어 있습니다. 운영 설정을 확인해주세요.",
        )
    bot = _get_bot_or_404(db, bot_id, current_user)
    _require_nlu_training_version_exists(db, bot.id, version_id)

    existing = db.scalar(
        select(ChannelQueueEvent)
        .where(
            ChannelQueueEvent.channel_type == NLU_TRAINING_QUEUE_CHANNEL,
            ChannelQueueEvent.bot_id == bot.id,
            ChannelQueueEvent.bot_version_id == version_id,
            ChannelQueueEvent.status.in_(("queued", "processing")),
            ChannelQueueEvent.deleted_at.is_(None),
        )
        .order_by(ChannelQueueEvent.created_at.asc())
    )
    if existing is not None:
        return success_response(request, _public_hub_payload(bot, _serialize_nlu_training_job(existing)))

    _get_version_or_404(db, bot.id, version_id)
    payload_json = payload.model_dump(mode="json") if payload is not None else {}
    queue_event = ChannelQueueEvent(
        room_id=None,
        request_message_id=None,
        channel_type=NLU_TRAINING_QUEUE_CHANNEL,
        bot_id=bot.id,
        bot_version_id=version_id,
        participant_id=current_user.login_id,
        intent_name="NLU 학습",
        sender_system="Aidot Studio",
        receiver="NLU Training Worker",
        priority="normal",
        receive_status="received",
        status="queued",
        parameter_json={
            "operation": NLU_TRAINING_QUEUE_OPERATION,
            "requested_by_user_id": str(current_user.id),
            "requested_by_login_id": current_user.login_id,
            "payload": payload_json,
        },
        result_json={"operation": NLU_TRAINING_QUEUE_OPERATION},
    )
    db.add(queue_event)
    db.flush()
    handoff_version_write(db, version_id, queue_event.id)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="bot.version.nlu.train.queued",
        target_type="bot_version",
        target_id=version_id,
        after_json={
            "queue_event_id": str(queue_event.id),
            "status": "queued",
            "operation": NLU_TRAINING_QUEUE_OPERATION,
        },
    )
    db.commit()
    db.refresh(queue_event)
    logger.info(
        "NLU training job queued.",
        extra={
            "event": "nlu.training.queued",
            "extra_data": {
                "queue_event_id": str(queue_event.id),
                "bot_id": str(bot.id),
                "version_id": str(version_id),
            },
        },
    )
    return success_response(request, _public_hub_payload(bot, _serialize_nlu_training_job(queue_event)))


@router.get("/{bot_id}/versions/{version_id}/nlu/train/jobs/{job_id}")
def get_version_nlu_training_job(
    bot_id: UUID,
    version_id: UUID,
    job_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    _require_nlu_training_version_exists(db, bot.id, version_id)
    queue_event = db.scalar(
        select(ChannelQueueEvent).where(
            ChannelQueueEvent.id == job_id,
            ChannelQueueEvent.channel_type == NLU_TRAINING_QUEUE_CHANNEL,
            ChannelQueueEvent.bot_id == bot.id,
            ChannelQueueEvent.bot_version_id == version_id,
            ChannelQueueEvent.deleted_at.is_(None),
        )
    )
    if queue_event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="학습 작업을 찾을 수 없습니다.")
    return success_response(request, _public_hub_payload(bot, _serialize_nlu_training_job(queue_event)))


def recover_interrupted_nlu_training_events(db: Session) -> int:
    events = db.scalars(
        select(ChannelQueueEvent).where(
            ChannelQueueEvent.channel_type == NLU_TRAINING_QUEUE_CHANNEL,
            ChannelQueueEvent.status == "processing",
            ChannelQueueEvent.deleted_at.is_(None),
        )
    ).all()
    for queue_event in events:
        if quarantine_interrupted_training(db, queue_event):
            _set_nlu_training_queue_status(queue_event, "failed", error_message="샘플 버전 writer의 종료 확인이 필요합니다.",
                result_json={**dict(queue_event.result_json or {}), "recovery_reason": "sample_version_writer_unknown"})
            db.add(queue_event)
            continue
        previous_result = dict(queue_event.result_json or {})
        _set_nlu_training_queue_status(
            queue_event,
            "queued",
            receive_status="received",
            result_json={
                **previous_result,
                "recovered_at": datetime.now(timezone.utc).isoformat(),
                "recovery_reason": "nlu_training_worker_restarted",
            },
        )
        db.add(queue_event)
    if events:
        db.commit()
    return len(events)


def _nlu_training_error_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False, default=str)
    return str(error) or type(error).__name__


def _process_nlu_training_queue_event(
    db: Session,
    queue_event: ChannelQueueEvent,
) -> dict[str, object]:
    parameter_json = dict(queue_event.parameter_json or {})
    requested_by_user_id = parameter_json.get("requested_by_user_id")
    try:
        user_id = UUID(str(requested_by_user_id))
    except (TypeError, ValueError):
        user_id = None
    current_user = db.get(User, user_id) if user_id is not None else None
    if current_user is None:
        message = "학습 요청 사용자를 찾을 수 없습니다."
        _set_nlu_training_queue_status(queue_event, "failed", error_message=message)
        db.add(queue_event)
        db.commit()
        return _serialize_nlu_training_job(queue_event)

    try:
        mutation_token = claim_training_version_write(db, queue_event)
    except MutationBusy:
        db.rollback()
        return _serialize_nlu_training_job(queue_event)
    except PinConflict as error:
        db.rollback()
        _set_nlu_training_queue_status(queue_event, "failed", error_message=str(error))
        db.add(queue_event)
        db.commit()
        return _serialize_nlu_training_job(queue_event)

    started_at = datetime.now(timezone.utc)
    _set_nlu_training_queue_status(
        queue_event,
        "processing",
        result_json={
            "operation": NLU_TRAINING_QUEUE_OPERATION,
            "started_at": started_at.isoformat(),
        },
    )
    db.add(queue_event)
    db.commit()
    queue_event_id = queue_event.id
    worker_request = type(
        "QueueWorkerRequest",
        (),
        {
            "client": None,
            "state": type("QueueWorkerState", (), {"request_id": f"queue:{queue_event_id}"})(),
        },
    )()

    try:
        payload_value = parameter_json.get("payload")
        payload = VersionNluTrainRequest.model_validate(payload_value) if isinstance(payload_value, dict) and payload_value else None
        with training_version_write(db, mutation_token):
            response = _run_version_nlu_training(
                bot_id=queue_event.bot_id,
                version_id=queue_event.bot_version_id,
                request=worker_request,
                payload=payload,
                current_user=current_user,
                db=db,
            )
        manifest = _compact_nlu_training_manifest(response.get("data") if isinstance(response, dict) else None)
        completed_at = datetime.now(timezone.utc)
        queue_event = db.get(ChannelQueueEvent, queue_event_id)
        if queue_event is None:
            raise RuntimeError("완료할 학습 Queue 이력을 찾을 수 없습니다.")
        _set_nlu_training_queue_status(
            queue_event,
            "completed",
            result_json={
                "operation": NLU_TRAINING_QUEUE_OPERATION,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_ms": max(0, round((completed_at - started_at).total_seconds() * 1000)),
                "manifest": manifest,
            },
        )
        db.add(queue_event)
        db.commit()
        logger.info(
            "NLU training job completed.",
            extra={
                "event": "nlu.training.completed",
                "extra_data": {
                    "queue_event_id": str(queue_event.id),
                    "bot_id": str(queue_event.bot_id),
                    "version_id": str(queue_event.bot_version_id),
                },
            },
        )
        return _serialize_nlu_training_job(queue_event)
    except Exception as error:
        db.rollback()
        message = _nlu_training_error_message(error)
        queue_event = db.get(ChannelQueueEvent, queue_event_id)
        if queue_event is None:
            raise
        failed_at = datetime.now(timezone.utc)
        _set_nlu_training_queue_status(
            queue_event,
            "failed",
            result_json={
                "operation": NLU_TRAINING_QUEUE_OPERATION,
                "started_at": started_at.isoformat(),
                "failed_at": failed_at.isoformat(),
                "elapsed_ms": max(0, round((failed_at - started_at).total_seconds() * 1000)),
                "error_type": type(error).__name__,
            },
            error_message=message,
        )
        db.add(queue_event)
        _write_audit_log(
            db,
            worker_request,
            current_user,
            action_type="bot.version.nlu.train.failed",
            target_type="bot_version",
            target_id=queue_event.bot_version_id,
            after_json={
                "queue_event_id": str(queue_event.id),
                "status": "failed",
                "error_type": type(error).__name__,
                "error_message": message,
            },
        )
        db.commit()
        logger.exception(
            "NLU training job failed.",
            extra={
                "event": "nlu.training.failed",
                "extra_data": {
                    "queue_event_id": str(queue_event.id),
                    "bot_id": str(queue_event.bot_id),
                    "version_id": str(queue_event.bot_version_id),
                    "error_type": type(error).__name__,
                    "error_message": message,
                },
            },
        )
        return _serialize_nlu_training_job(queue_event)


def process_queued_nlu_training_events(
    db: Session,
    *,
    limit: int = 1,
) -> list[dict[str, object]]:
    queue_events = db.scalars(
        select(ChannelQueueEvent)
        .where(
            ChannelQueueEvent.channel_type == NLU_TRAINING_QUEUE_CHANNEL,
            ChannelQueueEvent.status == "queued",
            ChannelQueueEvent.deleted_at.is_(None),
        )
        .order_by(ChannelQueueEvent.created_at.asc())
        .limit(max(1, limit))
    ).all()
    return [_process_nlu_training_queue_event(db, queue_event) for queue_event in queue_events]


@router.post("/{bot_id}/versions/{version_id}/nlu/ml/tokenize")
def tokenize_version_ml_intents(
    bot_id: UUID,
    version_id: UUID,
    payload: MlIntentTokenizeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    token_context = build_learning_token_context(version.version_json, version_settings=_get_version_settings(bot, version))
    try:
        items = tokenize_texts_for_deep_learning_lite(
            payload.utterances,
            token_context["canonical_map"],
            token_context["surface_canonical_map"],
            token_context["ignore_terms"],
            token_context["ignore_regexes"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return success_response(
        request,
        {
            "morph_analyzer": "kiwipiepy",
            "items": items,
        },
    )


@router.post("/{bot_id}/versions/{version_id}/nlu/ml/configure")
def configure_version_ml_intents(
    bot_id: UUID,
    version_id: UUID,
    payload: MlIntentConfigureRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    try:
        result = configure_intents_with_deep_learning_lite(
            utterances=payload.utterances,
            target_count=payload.target_count,
            target_count_policy=payload.target_count_policy,
            seed_intents=[seed.model_dump() for seed in payload.seed_intents],
            version_document=version.version_json,
            version_settings=_get_version_settings(bot, version),
            language=_bot_language(bot),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return success_response(request, result)


@router.post("/{bot_id}/versions/{version_id}/nlu/llm/configure")
def configure_version_llm_intents(
    bot_id: UUID,
    version_id: UUID,
    payload: LlmIntentConfigureRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    request_config = {
        "llm_provider": payload.llm_provider,
        "llm_model": payload.llm_model,
        "llm_base_url": payload.llm_base_url,
    }
    configure_config = {
        **ai_config,
        **{key: value for key, value in request_config.items() if value is not None},
    }

    try:
        result = configure_intents_with_llm(
            provider=_safe_text_value(configure_config.get("llm_provider") or "groq"),
            model=_safe_text_value(configure_config.get("llm_model") or "llama-3.3-70b-versatile"),
            api_key=None,
            base_url=_safe_text_value(configure_config.get("llm_base_url")),
            timeout_seconds=None,
            utterances=payload.utterances,
            target_count=payload.target_count,
            target_count_policy=payload.target_count_policy,
            dictionary_terms=payload.dictionary_terms,
            entity_terms=payload.entity_terms,
            language=_bot_language(bot),
        )
    except LlmClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return success_response(
        request,
        {
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "target_count": payload.target_count,
            "target_count_policy": payload.target_count_policy,
            "groups": [
                {
                    "id": f"llm-{index + 1}",
                    "name": group.name,
                    "answer": group.answer,
                    "utterances": group.utterances,
                    "reason": group.reason,
                }
                for index, group in enumerate(result.groups)
            ],
            "raw_content": result.raw_content,
        },
    )


@router.post("/{bot_id}/versions/{version_id}/nlu/semantic/configure")
def configure_version_semantic_intents(
    bot_id: UUID,
    version_id: UUID,
    payload: SemanticIntentConfigureRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    base_ai_config = dict(_get_version_ai_config(bot, version))
    vector_connections = base_ai_config.get("vector_connections")
    intent_connection = vector_connections.get("intent") if isinstance(vector_connections, dict) else {}
    intent_connection = intent_connection if isinstance(intent_connection, dict) else {}
    custom_endpoint_url = str(intent_connection.get("endpoint_url") or intent_connection.get("endpointUrl") or "").strip()
    custom_index_name = str(intent_connection.get("index_name") or intent_connection.get("indexName") or "").strip()
    custom_api_key = str(intent_connection.get("api_key") or intent_connection.get("apiKey") or "").strip()
    ai_config = {
        "nlu_type": "semantic_vector",
        "vector_connections": {
            "intent": {
                "enabled": True,
                "endpoint_url": custom_endpoint_url or f"{settings.aidot_vector_worker_base_url.rstrip('/')}/intent/search",
                "index_name": custom_index_name or "aidot-intent",
                "api_key": custom_api_key,
            }
        },
    }
    vector_config = intent_vector_config(ai_config)
    client = IntentVectorSearchClient(vector_config)

    try:
        result = client.configure_intents(
            bot_id=str(bot.id),
            version_id=str(version.id),
            utterances=payload.utterances,
            target_count=payload.target_count,
            target_count_policy=payload.target_count_policy,
            dictionary_terms=payload.dictionary_terms,
            entity_terms=[],
            scoring=payload.scoring,
        )
    except VectorSearchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    raw_groups = result.get("groups") if isinstance(result, dict) else []
    groups: list[dict[str, object]] = []
    if isinstance(raw_groups, list):
        for index, item in enumerate(raw_groups):
            if not isinstance(item, dict):
                continue
            raw_utterances = item.get("utterances")
            utterances = [str(value).strip() for value in raw_utterances if str(value).strip()] if isinstance(raw_utterances, list) else []
            name = _safe_text_value(item.get("name")) or f"의도 {index + 1}"
            groups.append(
                {
                    "id": _safe_text_value(item.get("id")) or f"semantic-{index + 1}",
                    "name": name,
                    "answer": _safe_text_value(item.get("answer")) or f"{name}에 대해 안내드리겠습니다.",
                    "utterances": utterances,
                    "reason": _safe_text_value(item.get("reason")) or "",
                }
            )

    return success_response(
        request,
        {
            "provider": _safe_text_value(result.get("provider")) if isinstance(result, dict) else "vector-worker",
            "model": _safe_text_value(result.get("model")) if isinstance(result, dict) else "",
            "latency_ms": 0,
            "target_count": payload.target_count,
            "target_count_policy": payload.target_count_policy,
            "groups": groups,
            "diagnostics": result.get("diagnostics") if isinstance(result, dict) else {},
        },
    )


@router.post("/{bot_id}/versions/{version_id}/nlu/llm/test")
def test_version_llm_intent(
    bot_id: UUID,
    version_id: UUID,
    payload: LlmIntentTestRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    vector_documents = _build_intent_vector_documents(version.version_json)
    if not vector_documents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM 의도 분류에 사용할 의도와 T 학습문장이 없습니다.",
        )

    try:
        result = classify_intent_with_llm(
            provider=_safe_text_value(ai_config.get("llm_provider")),
            model=_safe_text_value(ai_config.get("llm_model")),
            api_key=None,
            base_url=_safe_text_value(ai_config.get("llm_base_url")),
            timeout_seconds=None,
            query=payload.utterance,
            intents=vector_documents,
            top_k=payload.top_k,
            dictionary_terms=_build_llm_dictionary_terms(version.version_json),
            entity_terms=_build_llm_entity_terms(version.version_json),
            language=_bot_language(bot),
        )
    except LlmClientError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    candidates = [
        {
            "intent_id": item.intent_id,
            "intentId": item.intent_id,
            "intent_name": item.intent_name,
            "intentName": item.intent_name,
            "confidence": round(item.confidence, 4),
            "reason": item.reason,
        }
        for item in result.candidates
    ]
    return success_response(
        request,
        _public_hub_payload(bot, {
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "utterance": payload.utterance,
            "top_k": payload.top_k,
            "candidates": candidates,
            "raw_content": result.raw_content,
        }),
    )


@router.post("/{bot_id}/versions/{version_id}/nlu/ml/test")
def test_version_ml_intent(
    bot_id: UUID,
    version_id: UUID,
    payload: LlmIntentTestRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ml_settings = _get_ml_settings(bot, version)
    score_cutoff = _float_setting(ml_settings.get("cutOffScore"), 0.75)
    similar_intent_score = _float_setting(ml_settings.get("similarIntentScore"), 0.85)
    max_intent_results = max(1, _int_setting(ml_settings.get("maxIntentResults"), 3))
    top_k = max(1, min(payload.top_k or max_intent_results, max_intent_results))
    scores = score_deep_learning_lite_model(
        version,
        payload.utterance,
        version_settings=_get_version_settings(bot, version),
        language=_bot_language(bot),
    )
    candidates = [
        {
            "intent_id": str(item.get("dialog_id") or ""),
            "intentId": str(item.get("dialog_id") or ""),
            "intent_name": str(item.get("dialog_name") or ""),
            "intentName": str(item.get("dialog_name") or ""),
            "confidence": round(float(item.get("score") or 0.0), 4),
            "reason": ", ".join(str(feature) for feature in item.get("features") or []),
        }
        for item in scores[:top_k]
    ]
    return success_response(
        request,
        _public_hub_payload(bot, {
            "provider": "deep-learning-lite",
            "model": "deep_learning_lite",
            "latency_ms": 0,
            "utterance": payload.utterance,
            "top_k": top_k,
            "cutoff_score": score_cutoff,
            "similar_intent_score": similar_intent_score,
            "max_intent_results": max_intent_results,
            "candidates": candidates,
        }),
    )

@router.post("/{bot_id}/versions/{version_id}/nlu/semantic/test")
def test_version_semantic_intent(
    bot_id: UUID,
    version_id: UUID,
    payload: LlmIntentTestRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    vector_documents = _build_intent_vector_documents(version.version_json)
    if not vector_documents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Semantic 의도 분류에 사용할 의도와 T 학습문장이 없습니다.",
        )

    vector_config = intent_vector_config(ai_config)
    exact_match = _semantic_exact_intent_lookup(vector_documents).get(_compact_semantic_text(payload.utterance))
    if exact_match:
        return success_response(
            request,
            _public_hub_payload(bot, {
                "provider": "vector-worker",
                "model": _safe_text_value(ai_config.get("nlu_model") or ai_config.get("nlu_engine"))
                or "semantic_engine_default",
                "latency_ms": 0,
                "utterance": payload.utterance,
                "top_k": payload.top_k,
                "candidates": [
                    {
                        "intent_id": exact_match["dialog_id"],
                        "intentId": exact_match["dialog_id"],
                        "intent_name": exact_match["dialog_name"],
                        "intentName": exact_match["dialog_name"],
                        "confidence": 1.0,
                        "reason": exact_match.get("matched_text") or exact_match.get("source") or "exact",
                    }
                ],
            }),
        )
    label_match = _semantic_label_intent_match(vector_documents, payload.utterance)
    if label_match:
        return success_response(
            request,
            _public_hub_payload(bot, {
                "provider": "vector-worker",
                "model": _safe_text_value(ai_config.get("nlu_model") or ai_config.get("nlu_engine"))
                or "semantic_engine_default",
                "latency_ms": 0,
                "utterance": payload.utterance,
                "top_k": payload.top_k,
                "candidates": [
                    {
                        "intent_id": label_match["dialog_id"],
                        "intentId": label_match["dialog_id"],
                        "intent_name": label_match["dialog_name"],
                        "intentName": label_match["dialog_name"],
                        "confidence": round(float(label_match["score"]), 4),
                        "reason": "intent-label-match",
                    }
                ],
            }),
        )
    try:
        matches = IntentVectorSearchClient(vector_config).search(
            bot_id=str(bot.id),
            version_id=str(version.id),
            query=payload.utterance,
            top_k=payload.top_k,
            dictionary_terms=_build_llm_dictionary_terms(version.version_json),
        )
    except VectorSearchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    candidates = [
        {
            "intent_id": item.intent_id,
            "intentId": item.intent_id,
            "intent_name": item.intent_name,
            "intentName": item.intent_name,
            "confidence": round(item.score, 4),
            "reason": item.matched_text,
        }
        for item in matches
    ]
    return success_response(
        request,
        _public_hub_payload(bot, {
            "provider": "vector-worker",
            "model": _safe_text_value(ai_config.get("nlu_model") or ai_config.get("nlu_engine")) or "semantic_engine_default",
            "latency_ms": 0,
            "utterance": payload.utterance,
            "top_k": payload.top_k,
            "candidates": candidates,
        }),
    )


def _answer_rag_variable_prefix(ai_config: dict[str, object]) -> str:
    answer_mode = str(ai_config.get("answer_mode") or ai_config.get("answerMode") or "").strip()
    if answer_mode == "semantic_rag":
        return "_semantic"
    if answer_mode == "llm_rag":
        return "_llm"
    return ""


def _answer_rag_variable_prefixes(prefix: str) -> list[str]:
    if prefix == "_semantic":
        return ["_semantic", "_rag"]
    return [prefix] if prefix else []


def _answer_match_value(
    match: object,
    *,
    fallback_intent_id: str = "",
    fallback_intent_name: str = "",
) -> dict[str, object]:
    if isinstance(match, dict):
        metadata = match.get("metadata") if isinstance(match.get("metadata"), dict) else {}
        intent_id = _safe_text_value(match.get("intentId") or match.get("intent_id") or metadata.get("intentId") or metadata.get("intent_id") or fallback_intent_id)
        intent_name = _safe_text_value(match.get("intentName") or match.get("intent_name") or metadata.get("intentName") or metadata.get("intent_name") or fallback_intent_name)
        source_type = _safe_text_value(match.get("sourceType") or match.get("source_type") or metadata.get("sourceType") or metadata.get("source_type"))
        source_title = _safe_text_value(
            match.get("sourceTitle")
            or match.get("source_title")
            or metadata.get("sourceTitle")
            or metadata.get("source_title")
            or metadata.get("fileName")
            or metadata.get("file_name")
            or match.get("title")
        )
        return {
            "documentId": _safe_text_value(match.get("documentId") or match.get("document_id")),
            "title": _safe_text_value(match.get("title")),
            "text": _safe_text_value(match.get("text")),
            "score": round(float(match.get("score") or 0.0), 6),
            "intentId": intent_id,
            "intentName": intent_name,
            "sourceType": source_type,
            "sourceTitle": source_title,
            "page": _safe_text_value(match.get("page") or metadata.get("page") or metadata.get("pageNo") or metadata.get("page_no")),
            "metadata": metadata,
        }

    metadata_value = getattr(match, "metadata", None)
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
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


def _precomputed_answer_match(version: BotVersion, intent_id: str, intent_name: str) -> dict[str, object] | None:
    version_json = normalize_version_document(version.version_json)
    system_config = version_json.get("system_config") if isinstance(version_json.get("system_config"), dict) else {}
    answer_training = system_config.get("answer_training") if isinstance(system_config.get("answer_training"), dict) else {}
    precomputed = answer_training.get("precomputed_answers") if isinstance(answer_training.get("precomputed_answers"), dict) else {}
    by_intent_id = precomputed.get("by_intent_id") if isinstance(precomputed.get("by_intent_id"), dict) else {}
    by_intent_name = precomputed.get("by_intent_name") if isinstance(precomputed.get("by_intent_name"), dict) else {}
    value = by_intent_id.get(intent_id) if intent_id else None
    if not isinstance(value, dict):
        value = by_intent_name.get(_compact_semantic_text(intent_name)) if intent_name else None
    return _answer_match_value(value, fallback_intent_id=intent_id, fallback_intent_name=intent_name) if isinstance(value, dict) else None


def _answer_rag_variables(prefix: str, values: list[dict[str, object]]) -> dict[str, object]:
    first = values[0] if values else {}
    score = first.get("score") if first else ""
    variables: dict[str, object] = {}
    for item_prefix in _answer_rag_variable_prefixes(prefix):
        variables.update(
            {
                f"${item_prefix}_answers": values,
                f"${item_prefix}_answer_text": str(first.get("text") or "") if first else "",
                f"${item_prefix}_answer_score": f"{float(score):.4f}" if score != "" else "",
                f"${item_prefix}_answer_intent_id": str(first.get("intentId") or "") if first else "",
                f"${item_prefix}_answer_intent_name": str(first.get("intentName") or "") if first else "",
                f"${item_prefix}_answer_source_type": str(first.get("sourceType") or "") if first else "",
                f"${item_prefix}_answer_source_title": str(first.get("sourceTitle") or "") if first else "",
                f"${item_prefix}_answer_page": str(first.get("page") or "") if first else "",
            }
        )
    return variables


DEFAULT_LLM_ANSWER_SYSTEM_PROMPT = "사용자 질문에 답변한다."
LLM_JSON_ANSWER_FORMAT_PROMPT = "응답은 JSON 객체 하나만 반환한다. 형식: {\"answer\":\"...\"}"


def _find_dialog_in_version(version: BotVersion, dialog_id: str, dialog_name: str = "") -> dict[str, object]:
    version_json = normalize_version_document(version.version_json)
    dialogs = version_json.get("dialogs") if isinstance(version_json.get("dialogs"), list) else []
    normalized_name = _compact_semantic_text(dialog_name)
    for item in dialogs:
        if not isinstance(item, dict):
            continue
        if dialog_id and str(item.get("id") or "") == dialog_id:
            return item
        if normalized_name and normalized_name in {
            _compact_semantic_text(str(item.get("name") or "")),
            _compact_semantic_text(str(item.get("displayName") or "")),
        }:
            return item
    return {}


def _version_conversation_defaults(bot: Bot, version: BotVersion) -> dict[str, object]:
    version_settings = _get_version_settings(bot, version)
    conversation_defaults = version_settings.get("conversationDefaults")
    return conversation_defaults if isinstance(conversation_defaults, dict) else {}


def _llm_answer_system_prompt_for_version(bot: Bot, version: BotVersion, selected_dialog: dict[str, object]) -> str:
    dialog_prompt = _safe_text_value(
        selected_dialog.get("llmAnswerPrompt")
        or selected_dialog.get("llm_answer_prompt")
        or selected_dialog.get("answerPrompt")
        or selected_dialog.get("answer_prompt")
    )
    if dialog_prompt:
        base_prompt = dialog_prompt
    else:
        conversation_defaults = _version_conversation_defaults(bot, version)
        llm_answer = conversation_defaults.get("llmAnswer") if isinstance(conversation_defaults.get("llmAnswer"), dict) else {}
        base_prompt = _safe_text_value(
            llm_answer.get("systemPrompt")
            or llm_answer.get("system_prompt")
            or llm_answer.get("prompt")
        ) if isinstance(llm_answer, dict) else ""
    return " ".join(
        item
        for item in [
            base_prompt or DEFAULT_LLM_ANSWER_SYSTEM_PROMPT,
            LLM_JSON_ANSWER_FORMAT_PROMPT,
        ]
        if item
    )


def _generate_llm_answer_text(
    ai_config: dict[str, object],
    system_prompt: str,
    query: str,
    *,
    intent_id: str = "",
    intent_name: str = "",
) -> str:
    provider = _safe_text_value(ai_config.get("llm_provider"))
    model = _safe_text_value(ai_config.get("llm_model"))
    base_url = _safe_text_value(ai_config.get("llm_base_url"))
    config = resolve_llm_provider_config(provider, model, base_url=base_url)
    client = LlmChatClient(config)
    user_prompt_parts = [f"사용자 질문: {query}"]
    if intent_id or intent_name:
        user_prompt_parts.append(f"매칭 의도 ID: {intent_id}")
        user_prompt_parts.append(f"매칭 의도명: {intent_name}")
    user_prompt_parts.append("위 사용자 질문에 대한 답변을 생성하세요.")
    result = client.chat(
        system_prompt=system_prompt,
        user_prompt="\n".join(part for part in user_prompt_parts if part.strip()),
        json_mode=True,
    )
    try:
        parsed = json.loads(result.content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", result.content, flags=re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    return _safe_text_value(parsed.get("answer") if isinstance(parsed, dict) else "")


@router.post("/{bot_id}/versions/{version_id}/answers/llm/generate")
def generate_version_llm_answer(
    bot_id: UUID,
    version_id: UUID,
    payload: dict[str, object],
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    answer_mode = _safe_text_value(ai_config.get("answer_mode") or ai_config.get("answerMode"))
    if answer_mode != "llm":
        return success_response(
            request,
            {
                "answer_mode": answer_mode,
                "prefix": "",
                "matches": [],
                "variables": {},
            },
        )

    query = _safe_text_value(payload.get("query") or payload.get("utterance"))
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="답변을 생성할 사용자 발화가 없습니다.")

    intent_id = _safe_text_value(payload.get("intentId") or payload.get("intent_id"))
    intent_name = _safe_text_value(payload.get("intentName") or payload.get("intent_name"))
    selected_dialog = _find_dialog_in_version(version, intent_id, intent_name)
    system_prompt = _llm_answer_system_prompt_for_version(bot, version, selected_dialog)
    try:
        answer = _generate_llm_answer_text(
            ai_config,
            system_prompt,
            query,
            intent_id=intent_id,
            intent_name=intent_name,
        )
    except (LlmClientError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    values = [{
        "text": answer,
        "score": 1.0 if answer else 0.0,
        "intentId": intent_id,
        "intentName": intent_name,
        "sourceType": "llm",
        "sourceTitle": "LLM Engine 답변",
        "metadata": {
            "llmGenerated": True,
            "llmProvider": _safe_text_value(ai_config.get("llm_provider")),
            "llmModel": _safe_text_value(ai_config.get("llm_model")),
        },
    }]
    return success_response(
        request,
        {
            "answer_mode": answer_mode,
            "prefix": "_llm",
            "matches": values,
            "variables": _answer_rag_variables("_llm", values),
            "answer_source": "llm",
        },
    )


@router.post("/{bot_id}/versions/{version_id}/answers/rag/search")
def search_version_rag_answers(
    bot_id: UUID,
    version_id: UUID,
    payload: dict[str, object],
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    answer_mode = str(ai_config.get("answer_mode") or ai_config.get("answerMode") or "").strip()
    prefix = _answer_rag_variable_prefix(ai_config)
    if not prefix:
        return success_response(
            request,
            {
                "answer_mode": answer_mode,
                "prefix": "",
                "matches": [],
                "variables": {},
            },
        )

    query = str(payload.get("query") or payload.get("utterance") or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="검색할 사용자 발화가 없습니다.")

    raw_top_k = payload.get("topK") or payload.get("top_k") or 3
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError):
        top_k = 3
    top_k = max(1, min(10, top_k))

    intent_id = str(payload.get("intentId") or payload.get("intent_id") or "").strip()
    intent_name = str(payload.get("intentName") or payload.get("intent_name") or "").strip()
    precomputed_match = _precomputed_answer_match(version, intent_id, intent_name)
    if precomputed_match is not None:
        values = [precomputed_match]
        return success_response(
            request,
            {
                "answer_mode": answer_mode,
                "prefix": prefix,
                "matches": values,
                "variables": _answer_rag_variables(prefix, values),
                "answer_source": "precomputed",
            },
        )

    config = answer_vector_config(ai_config)
    if not config.is_ready:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Answer Vector DB 연결 설정이 완료되지 않았습니다. 누락: {', '.join(config.missing_fields)}",
        )

    try:
        client = AnswerVectorSearchClient(config)
        embedding_options = _answer_training_embedding_options_from_state(version)
        matches = client.search(
            bot_id=str(bot.id),
            version_id=str(version.id),
            query=query,
            top_k=top_k,
            intent_ids=[intent_id] if intent_id else None,
            embedding_provider=embedding_options["embedding_provider"],
            embedding_model=embedding_options["embedding_model"],
        )
        if intent_id and not matches:
            matches = client.search(
                bot_id=str(bot.id),
                version_id=str(version.id),
                query=query,
                top_k=top_k,
                embedding_provider=embedding_options["embedding_provider"],
                embedding_model=embedding_options["embedding_model"],
            )
    except VectorSearchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    values = [
        _answer_match_value(match, fallback_intent_id=intent_id, fallback_intent_name=intent_name)
        for match in matches
    ]
    return success_response(
        request,
        {
            "answer_mode": answer_mode,
            "prefix": prefix,
            "matches": values,
            "variables": _answer_rag_variables(prefix, values),
            "answer_source": "vector_fallback",
        },
    )


@router.get("/{bot_id}/versions/{version_id}/nlu/model")
def get_version_nlu_model(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    return success_response(request, _public_hub_payload(bot, get_deep_learning_lite_model_manifest(bot.id, version.id)))


@router.get("/{bot_id}/versions/{version_id}/export")
def export_version_package(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    answer_vector_index = _export_answer_vector_index(bot, version, ai_config)
    return success_response(
        request,
        _public_hub_payload(bot, {
            "schema_version": "aidot-version-package-v1",
            "bot": {
                "id": str(bot.id),
                "name": bot.name,
            },
            "version": {
                "id": str(version.id),
                "name": version.name,
                "version_no": version.version_no,
                "status": version.status,
                "comment": version.comment,
                "description": version.description,
            },
            "version_json": normalize_version_document(version.version_json),
            "answer_vector_index": answer_vector_index,
        }),
    )


@router.get("/{bot_id}/versions/{version_id}/aidot-package")
def export_aidot_bot_package(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    package = version_document_to_aidot_bot_package(
        _public_hub_payload(bot, version.version_json),
        bot={
            "id": str(bot.id),
            "name": bot.name,
            "description": bot.description,
            "locale": str((bot.data_json or {}).get("language") or "ko"),
        },
        version={
            "id": str(version.id),
            "name": version.name,
            "version_no": version.version_no,
        },
    )
    return success_response(request, package)


@router.post("/{bot_id}/versions/{version_id}/aidot-package")
@version_mutation("import")
def import_aidot_bot_package(
    bot_id: UUID,
    version_id: UUID,
    payload: dict[str, object],
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    if version.status == "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="운영 버전에는 Aidot 봇 패키지를 덮어쓸 수 없습니다. 먼저 운영을 해제해주세요.",
        )

    try:
        imported_document = aidot_bot_package_to_version_document(payload)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    current_document = normalize_version_document(version.version_json)
    imported_system_config = imported_document.get("system_config")
    imported_system_config = imported_system_config if isinstance(imported_system_config, dict) else {}
    current_system_config = current_document.get("system_config")
    current_system_config = current_system_config if isinstance(current_system_config, dict) else {}
    imported_document["apis"] = deepcopy(current_document.get("apis") or [])
    imported_document["system_config"] = {**deepcopy(current_system_config), **deepcopy(imported_system_config)}

    next_version_json = _prepare_version_document_for_save(imported_document, _get_nlu_training_state(version))
    scenario_validation = scenario_validation_from_version(next_version_json)
    if save_blocking_scenario_items(scenario_validation):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Aidot 패키지의 대화 설계 참조를 확인해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    before_json = _serialize_version(bot, version, db)
    _assign_version_document(version, next_version_json)
    db.add(version)
    sync_version_dialog_split_tables(
        db,
        bot,
        version,
        next_version_json,
        include_dialogs=True,
        include_graphs=True,
    )
    db.flush()
    after_json = _serialize_version(bot, version, db)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.aidot_package.import",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json=after_json,
    )
    db.commit()
    db.refresh(version)
    _purge_version_cache(version)
    return success_response(
        request,
        {
            "version": _serialize_version(bot, version, db),
            "compatibility": aidot_package_summary(payload),
        },
    )


@router.post("/{bot_id}/versions/{version_id}/answers/rag/import-index")
@version_mutation("rag_write")
def import_version_rag_answer_index(
    bot_id: UUID,
    version_id: UUID,
    payload: dict[str, object],
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    ai_config = _get_version_ai_config(bot, version)
    answer_vector_index = payload.get("answer_vector_index") or payload.get("payload")
    result = _import_answer_vector_index(bot, version, ai_config, answer_vector_index)
    if result is None:
        version_json = _mark_answer_training_reembed_required(
            version.version_json,
            "업로드 파일에 Answer Vector 인덱스가 없거나 가져오기에 실패했습니다.",
        )
        _assign_version_document(version, version_json)
        db.add(version)
        db.commit()
        _purge_version_cache(version)
        return success_response(request, {"imported": 0, "status": "reembed_required"})
    return success_response(request, {"status": "success", **result})


@router.delete("/{bot_id}/versions/{version_id}")
@version_mutation("delete")
def delete_version(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    version = _get_version_or_404(db, bot.id, version_id)
    before_json = _serialize_version(bot, version, db)

    if bot.active_version_id == version.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="운영 버전은 삭제할 수 없습니다. 먼저 운영을 해제해주세요.",
        )

    remaining_version_count = db.scalar(
        select(func.count(BotVersion.id)).where(
            BotVersion.bot_id == bot.id,
            BotVersion.deleted_at.is_(None),
        )
    ) or 0
    if remaining_version_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="마지막 버전은 삭제할 수 없습니다. 봇 삭제를 사용해주세요.",
        )

    _hard_delete_bot_version(db, version)
    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.delete",
        target_type="bot_version",
        target_id=version.id,
        before_json=before_json,
        after_json={
            "id": str(version.id),
            "deleted": True,
        },
    )
    db.commit()
    _purge_version_cache(version)

    return success_response(
        request,
        {
            "message": "버전이 삭제되었습니다.",
        },
    )


@router.post("/{bot_id}/versions/{version_id}/copy")
@version_mutation("copy")
def copy_version(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot = _get_bot_or_404(db, bot_id, current_user)
    source_version = _get_version_or_404(db, bot.id, version_id)
    next_version_no = _next_bot_version_no(db, bot.id)

    copied_document = _set_nlu_training_state(deepcopy(normalize_version_document(source_version.version_json)), None)
    copied_version = BotVersion(
        bot_id=bot.id,
        version_no=next_version_no,
        name=f"v{next_version_no}",
        description=source_version.description,
        status="draft",
        comment=source_version.comment,
        version_json=copied_document,
        copied_from_version_id=source_version.id,
        created_by=current_user.id,
    )
    _refresh_version_read_snapshot(copied_version, copied_document)
    db.add(copied_version)
    db.flush()
    ai_config = _get_version_ai_config(bot, source_version)
    if db.get(ChannelLlmVersionGuard, source_version.id) is not None or str(bot.id) == settings.persistent_llm_sample_bot_id:
        register_sample_version(db, copied_version.id)
        admit_version_write(db, copied_version, force=True)
    answer_vector_copy = _copy_answer_vector_index(bot, source_version, copied_version, ai_config)

    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.copy",
        target_type="bot_version",
        target_id=copied_version.id,
        before_json={"source_version_id": str(source_version.id)},
        after_json={
            **_serialize_version(bot, copied_version, db),
            "answer_vector_copy": answer_vector_copy,
        },
    )
    db.commit()
    db.refresh(copied_version)

    return success_response(request, _serialize_version(bot, copied_version, db))


@router.post("/{bot_id}/versions/{version_id}/activate")
def activate_version(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_version_operating_user(db, current_user)
    bot = _get_bot_or_404(db, bot_id, current_user)
    target_version = _get_version_or_404(db, bot.id, version_id)
    hub = db.get(BotHub, bot.id) if str((bot.data_json or {}).get("bot_kind") or "").lower() == "hub" else None
    is_button_hub = hub is not None and str(hub.call_method or "button").lower() == "button"
    if not is_button_hub and not _is_version_trained(target_version, db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="학습이 완료된 버전만 운영 버전으로 지정할 수 있습니다. 먼저 학습하기를 실행해주세요.",
        )
    scenario_validation = scenario_validation_from_version(target_version.version_json)
    if int(scenario_validation.get("error_count") or 0) > 0:
        detail = scenario_validation_error_detail(scenario_validation)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": f"대화 설계 오류가 있는 버전은 운영 버전으로 지정할 수 없습니다. {detail} 오류를 수정하고 다시 학습해주세요.",
                "scenario_validation": scenario_validation,
            },
        )

    before_json = {
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
    }
    _close_open_channel_rooms_for_version_change(db, request, current_user, bot, bot.active_version_id, target_version.id)

    previous_active_versions = db.scalars(
        select(BotVersion).where(
            BotVersion.bot_id == bot.id,
            BotVersion.id != target_version.id,
            BotVersion.status == "active",
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    for previous_active_version in previous_active_versions:
        previous_active_version.status = "testing"
        db.add(previous_active_version)

    bot.active_version_id = target_version.id
    target_version.status = "active"
    target_version.activated_at = datetime.now(timezone.utc)
    target_version.activated_by = current_user.id

    db.add(bot)
    db.add(target_version)
    db.flush()

    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.activate",
        target_type="bot_version",
        target_id=target_version.id,
        before_json=before_json,
        after_json={
            "active_version_id": str(bot.active_version_id),
            "version_id": str(target_version.id),
        },
    )
    db.commit()
    db.refresh(bot)
    db.refresh(target_version)

    return success_response(request, _serialize_version(bot, target_version, db))

@router.post("/{bot_id}/versions/{version_id}/deactivate")
def deactivate_version(
    bot_id: UUID,
    version_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    _require_version_operating_user(db, current_user)
    bot = _get_bot_or_404(db, bot_id, current_user)
    target_version = _get_version_or_404(db, bot.id, version_id)

    if bot.active_version_id != target_version.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="현재 운영 버전으로 지정된 버전만 해제할 수 있습니다.",
        )

    before_json = {
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
        "version_id": str(target_version.id),
        "version_status": target_version.status,
    }
    _close_open_channel_rooms_for_version_change(db, request, current_user, bot, bot.active_version_id, None)

    bot.active_version_id = None
    target_version.status = "testing"
    target_version.activated_at = None
    target_version.activated_by = None

    db.add(bot)
    db.add(target_version)
    db.flush()

    _write_audit_log(
        db,
        request,
        current_user,
        action_type="version.deactivate",
        target_type="bot_version",
        target_id=target_version.id,
        before_json=before_json,
        after_json={
            "active_version_id": None,
            "version_id": str(target_version.id),
            "version_status": target_version.status,
        },
    )
    db.commit()
    db.refresh(bot)
    db.refresh(target_version)

    return success_response(request, _serialize_version(bot, target_version, db))


# 지속 LLM 샘플의 인증/예산/시나리오 연결. 일반 단발 경로는 위의 기존 handler를 유지한다.
def _persistent_provider_transport():
    return BoundedJsonTransport()


def _persistent_license(db: Session, user: User):
    from datetime import date
    from app.services.license_policy import parse_license_date

    record = db.scalar(
        select(AdminLicense)
        .where(
            AdminLicense.organization_id == user.organization_id,
            AdminLicense.status == "active",
        )
        .order_by(AdminLicense.updated_at.desc())
        .with_for_update(read=True)
    )
    expires = (
        parse_license_date((record.payload_json or {}).get("expires_at"))
        if record
        else None
    )
    if record is None or (expires is not None and expires < date.today()):
        raise HTTPException(status_code=403, detail="유효한 라이선스가 필요합니다.")


def _persistent_execution(bot_id: UUID):
    if (
        not settings.persistent_llm_enabled
        or not settings.persistent_llm_execution_enabled
        or str(bot_id) != settings.persistent_llm_sample_bot_id
    ):
        raise HTTPException(
            status_code=409, detail="지속 대화 샘플 실행이 준비되지 않았습니다."
        )


def _persistent_policy(config=None):
    names = (
        "context_input_tokens",
        "max_output_tokens",
        "budget_amount",
        "generation_limit",
    )
    policy = {name: getattr(settings, "persistent_llm_" + name) for name in names}
    policy.update(
        scope=settings.persistent_llm_budget_scope,
        tariff_model=settings.persistent_llm_tariff_model,
        tariff_version=settings.persistent_llm_tariff_version,
        currency=settings.persistent_llm_currency,
        input_rate=settings.persistent_llm_input_rate_units_per_million,
        output_rate=settings.persistent_llm_output_rate_units_per_million,
    )
    if (
        any(
            type(policy[name]) is not int or not 0 < policy[name] < 2**63
            for name in names
        )
        or any(
            type(policy[name]) is not int or not 0 <= policy[name] < 2**63
            for name in ("input_rate", "output_rate")
        )
        or not isinstance(policy["scope"], str)
        or not 1 <= len(policy["scope"]) <= 100
        or not policy["tariff_version"]
        or len(policy["tariff_version"]) > 80
        or not re.fullmatch(r"[A-Z]{3}", policy["currency"] or "")
        or not policy["tariff_model"]
        or (config is not None and policy["tariff_model"] != config.model)
    ):
        raise HTTPException(
            status_code=409,
            detail="모델에 맞는 승인 실행 한도와 요금표 준비가 필요합니다.",
        )
    policy["amount_upper"] = (
        policy["context_input_tokens"] * policy["input_rate"] + 999999
    ) // 1000000 + (
        policy["max_output_tokens"] * policy["output_rate"] + 999999
    ) // 1000000
    if policy["amount_upper"] >= 2**63:
        raise HTTPException(status_code=409, detail="예약 금액 상한을 확인해야 합니다.")
    return policy


def _persistent_config(ai):
    from urllib.parse import urlsplit

    config = resolve_llm_provider_config(
        _safe_text_value(ai.get("llm_provider")),
        _safe_text_value(ai.get("llm_model")),
        base_url=_safe_text_value(ai.get("llm_base_url")),
    )
    endpoint = urlsplit(config.base_url)
    if (
        config.provider != "chatgpt"
        or not config.api_key
        or endpoint.scheme != "https"
        or not endpoint.netloc
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise HTTPException(
            status_code=409,
            detail="기존 OpenAI Provider·모델·서버 인증 연결을 확인해야 합니다.",
        )
    return config


def _persistent_profiles(document):
    from app.api.routes import channels

    profiles = {}
    dialogs = {
        str(row.get("id")): row
        for row in document.get("dialogs", [])
        if isinstance(row, dict)
    }
    for graph in document.get("dialog_flow_graphs", []):
        if not isinstance(graph, dict) or str(graph.get("dialogId")) not in dialogs:
            continue
        nodes = graph.get("nodes") or []
        links = graph.get("links") or []
        if (
            not isinstance(nodes, list)
            or not isinstance(links, list)
            or any(not isinstance(edge, dict) for edge in links)
        ):
            continue
        if (
            not nodes
            or any(
                not isinstance(node, dict)
                or node.get("kind") not in {"start", "talk", "end"}
                for node in nodes
            )
            or sum(node.get("kind") == "talk" for node in nodes) != 1
            or sum(node.get("kind") == "end" for node in nodes) != 1
            or sum(node.get("kind") == "start" for node in nodes) > 1
            or len(links) != len(nodes) - 1
        ):
            continue
        identifiers = [str(node.get("id") or "") for node in nodes]
        if "" in identifiers or len(set(identifiers)) != len(identifiers):
            continue
        current = channels._first_runtime_node_id(graph)
        visited = []
        valid = True
        while current:
            if current in visited:
                valid = False
                break
            visited.append(current)
            node = next((n for n in nodes if str(n.get("id")) == current), None)
            if node is None:
                valid = False
                break
            edges = [edge for edge in links if edge.get("sourceNodeId") == current]
            if node["kind"] == "end":
                if edges or (node.get("config") or {}).get("message"):
                    valid = False
                break
            if len(edges) != 1 or str(edges[0].get("sourcePort") or "next") != "next":
                valid = False
                break
            current = str(edges[0].get("targetNodeId") or "")
        # _first_runtime_node_id는 Start를 건너뛸 수 있어 연결이 없는 Start도 별도 확인한다.
        reachable = set(visited) | {
            str(n["id"]) for n in nodes if n.get("kind") == "start"
        }
        if (
            not valid
            or reachable != set(identifiers)
            or not visited
            or next(n for n in nodes if str(n["id"]) == visited[-1])["kind"] != "end"
        ):
            continue
        talk = next(n for n in nodes if n["kind"] == "talk")
        cfg = channels._talk_config_for_channel(
            dict(talk.get("config") or {}), "WEBCHAT"
        )
        if (
            str(cfg.get("messageType") or "text") != "text"
            or str(cfg.get("responseType") or "none") != "none"
        ):
            continue
        basic = cfg.get("basicMessages") or []
        messages = cfg.get("messages") or []
        if (
            not isinstance(basic, list)
            or not isinstance(messages, list)
            or any(not isinstance(value, str) for value in [*basic, *messages])
        ):
            continue
        template = " ".join([*basic, *messages])
        if not template.strip():
            continue
        dialog = dialogs[str(graph["dialogId"])]
        profiles[str(graph["dialogId"])] = {
            "dialog": deepcopy(dialog),
            "graph": deepcopy(graph),
            "uses_llm": bool(
                re.search(r"\{\{\s*\$?_?llm_(?:answers\b|answer_)", template)
            ),
        }
    return profiles


def _persistent_snapshot(db, bot, version, dialog_id):
    from app.api.routes import channels

    guard = db.get(ChannelLlmVersionGuard, version.id, populate_existing=True)
    if guard is None or guard.mutation_status != "idle":
        raise HTTPException(
            status_code=409,
            detail="샘플로 준비한 새 버전의 쓰기 종료 확인이 필요합니다.",
        )
    ai = _get_version_ai_config(bot, version)
    if str(ai.get("nlu_type") or "ml") in {
        "semantic",
        "semantic_vector",
        "semantic_external",
    } or str(ai.get("answer_mode") or "fixed") in {"semantic_rag", "llm_rag"}:
        raise HTTPException(
            status_code=409,
            detail="공유 검색 지식 불변성을 확인하기 전에는 지속 대화를 시작할 수 없습니다.",
        )
    defaults = deepcopy(_version_conversation_defaults(bot, version))
    detection = (
        defaults.get("intentDetection")
        if isinstance(defaults.get("intentDetection"), dict)
        else {}
    )
    if detection.get("preprocessModule") or detection.get("beforeSessionEndModule"):
        raise HTTPException(
            status_code=409,
            detail="첫 샘플은 외부 실행 없이 텍스트 Talk와 End로 끝나는 모듈을 사용합니다.",
        )
    document = normalize_version_document(version.version_json)
    profiles = _persistent_profiles(document)
    if dialog_id not in profiles:
        raise HTTPException(
            status_code=409, detail="텍스트 Talk와 End로 구성한 모듈을 선택해 주세요."
        )
    config = _persistent_config(ai)
    policy = _persistent_policy(config)
    selected = {
        "dialogs": [p["dialog"] for p in profiles.values()],
        "dialog_flow_graphs": [p["graph"] for p in profiles.values()],
    }
    prompts = {
        key: _llm_answer_system_prompt_for_version(bot, version, profile["dialog"])
        for key, profile in profiles.items()
    }
    return {
        "instructions": _llm_answer_system_prompt_for_version(bot, version, {}),
        "module_instructions": prompts,
        "resolved_llm_config": {
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
        },
        "ai_config": {
            key: deepcopy(ai.get(key))
            for key in (
                "nlu_type",
                "nlu_model",
                "answer_mode",
                "llm_provider",
                "llm_model",
            )
        },
        "document": selected,
        "profiles": {
            key: {
                "name": p["dialog"].get("displayName") or p["dialog"].get("name"),
                "uses_llm": p["uses_llm"],
            }
            for key, p in profiles.items()
        },
        "initial_dialog_id": dialog_id,
        "rate_policy": policy,
        "background": settings.persistent_llm_background,
        "knowledge": {"kind": "local_inline", "version_id": str(version.id)},
    }


def _persistent_scope(binding):
    return TurnScope(
        binding.organization_id,
        binding.group_id,
        binding.bot_id,
        binding.room_id,
        binding.principal_key,
        binding.generation,
        binding.version_id,
    )


def _persistent_binding(db, bot, current_user, room_id, generation=None):
    query = select(ChannelLlmBinding).where(
        ChannelLlmBinding.room_id == room_id,
        ChannelLlmBinding.bot_id == bot.id,
        ChannelLlmBinding.user_id == current_user.id,
        ChannelLlmBinding.organization_id == current_user.organization_id,
        ChannelLlmBinding.group_id == current_user.group_id,
    )
    if generation is not None:
        query = query.where(ChannelLlmBinding.generation == generation)
    binding = db.scalar(query.order_by(ChannelLlmBinding.generation.desc()).limit(1))
    if binding is None:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")
    return binding


def _persistent_access(db, scope, *, read_only=False):
    user = db.scalar(
        select(User)
        .where(User.id == UUID(scope.principal_key[5:]), User.deleted_at.is_(None))
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.status != "active"
        or user.organization_id != scope.org_id
        or user.group_id != scope.group_id
    ):
        raise HTTPException(status_code=403, detail="대화 이용 권한을 확인해야 합니다.")
    _persistent_license(db, user)
    binding = db.scalar(
        select(ChannelLlmBinding)
        .where(
            ChannelLlmBinding.room_id == scope.room_id,
            ChannelLlmBinding.generation == scope.generation,
            ChannelLlmBinding.principal_key == scope.principal_key,
        )
        .execution_options(populate_existing=True)
    )
    if binding is not None and binding.status == "revoked":
        raise HTTPException(
            status_code=403, detail="관리자가 이 대화의 이용을 중지했습니다."
        )
    if not read_only:
        _persistent_execution(scope.bot_id)


def _persistent_render(db, binding, dialog_id, answer, question):
    from app.api.routes import channels

    document = deepcopy(binding.snapshot_json["document"])
    profiles = _persistent_profiles(document)
    if dialog_id not in profiles:
        raise TurnConflict("고정된 모듈을 찾을 수 없습니다.")
    graph = profiles[dialog_id]["graph"]
    state = {
        "graphId": str(graph["id"]),
        "dialogId": dialog_id,
        "variables": _answer_rag_variables("_llm", [{"text": answer, "score": 1.0}]),
    }
    room = db.get(ChannelRoom, binding.room_id)
    bot = db.get(Bot, binding.bot_id)
    channels._set_runtime_system_variables(
        state,
        bot=bot,
        room=room,
        participant_id=str(binding.user_id),
        participant_name=room.participant_name,
        message=question,
    )
    outputs, state = channels._run_runtime(
        document, state, channels._first_runtime_node_id(graph), {}, "WEBCHAT"
    )
    if (
        len(outputs) != 1
        or outputs[0].get("type") != "text"
        or not str(outputs[0].get("text") or "").strip()
        or state.get("dialogEnded") is not True
    ):
        raise TurnConflict("서버 Talk의 최종 표출을 확인해야 합니다.")
    room.metadata_json = {
        **dict(room.metadata_json or {}),
        **state,
        "persistent_llm": {
            "owner_id": str(binding.user_id),
            "generation": binding.generation,
        },
    }
    db.add(room)
    db.flush()
    return str(outputs[0]["text"])


def _persistent_runtime_service(db, scope, *, event_id=None, read_only=False):
    from app.services.llm_client import LlmProviderConfig

    binding = db.scalar(
        select(ChannelLlmBinding).where(
            ChannelLlmBinding.room_id == scope.room_id,
            ChannelLlmBinding.generation == scope.generation,
            ChannelLlmBinding.principal_key == scope.principal_key,
        )
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="채팅방을 찾을 수 없습니다.")

    def usage(raw):
        policy = binding.snapshot_json["rate_policy"]
        evidence = raw["usage"]
        amount = (
            evidence["input_tokens"] * policy["input_rate"] + 999999
        ) // 1000000 + (
            evidence["output_tokens"] * policy["output_rate"] + 999999
        ) // 1000000
        return UsageEvidence(
            amount, policy["currency"], policy["tariff_version"], str(raw["id"])
        )

    def render(answer):
        event = db.get(ChannelQueueEvent, event_id)
        payload = (event.parameter_json or {}).get("payload") or {}
        turn = json.loads(payload["input"][-1]["content"])["untrusted_turn_references"]
        return _persistent_render(
            db,
            binding,
            str(turn["cga_dialog_id"]),
            answer,
            str(event.parameter_json["question"]),
        )

    client = None
    if not read_only:
        frozen = binding.snapshot_json["resolved_llm_config"]
        credentials = _persistent_config(
            {"llm_provider": frozen["provider"], "llm_model": frozen["model"]}
        )
        config = LlmProviderConfig(
            provider=frozen["provider"],
            model=frozen["model"],
            base_url=frozen["base_url"],
            api_key=credentials.api_key,
            timeout_seconds=30,
        )
        client = OpenAIConversations(config, transport=_persistent_provider_transport())
    return PersistentLlmService(
        client,
        usage_evaluator=usage,
        display_transform=render,
        access_validator=_persistent_access,
    )


def _persistent_accounts(db, binding):
    from sqlalchemy import text

    policy = binding.snapshot_json["rate_policy"]
    if policy != _persistent_policy():
        raise HTTPException(
            status_code=409, detail="대화 시작 시의 실행 예산·요금표를 확인해야 합니다."
        )
    ids = []
    for kind, key in (
        ("experiment", policy["scope"]),
        ("organization", policy["scope"] + ":" + str(binding.organization_id)),
    ):
        db.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "pllm-budget:" + kind + ":" + key},
        )
        account = db.scalar(
            select(LlmBudgetAccount).where(
                LlmBudgetAccount.scope_kind == kind, LlmBudgetAccount.scope_key == key
            )
        )
        if account is None:
            account = LlmBudgetAccount(
                scope_kind=kind,
                scope_key=key,
                currency=policy["currency"],
                tariff_version=policy["tariff_version"],
                limit_amount=policy["budget_amount"],
                limit_count=policy["generation_limit"],
            )
            db.add(account)
            db.flush()
        elif (
            account.currency != policy["currency"]
            or account.tariff_version != policy["tariff_version"]
            or account.unit_scale != 1000000
            or account.limit_amount != policy["budget_amount"]
            or account.limit_count != policy["generation_limit"]
        ):
            raise HTTPException(
                status_code=409,
                detail="기존 사용량을 보존하며 실행 한도를 재검토해야 합니다.",
            )
        ids.append(account.id)
    return tuple(ids)


def _persistent_room_payload(db, binding):
    room = db.get(ChannelRoom, binding.room_id)
    event = db.scalar(
        select(ChannelQueueEvent)
        .where(ChannelQueueEvent.binding_id == binding.id)
        .order_by(ChannelQueueEvent.created_at.desc())
        .limit(1)
    )
    return {
        "id": str(room.id),
        "generation": binding.generation,
        "version_id": str(binding.version_id),
        "status": binding.status,
        "scenario_ended": bool((room.metadata_json or {}).get("dialogEnded")),
        "dialog_id": binding.snapshot_json.get("initial_dialog_id"),
        "profiles": binding.snapshot_json.get("profiles", {}),
        "current_event_id": str(event.id) if event else None,
        "can_send": binding.status == "open"
        and (event is None or event.phase == "committed"),
    }


@router.post("/{bot_id}/persistent/rooms")
def start_persistent_room(
    bot_id: UUID,
    payload: PersistentRoomStartRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from uuid import uuid4
    from app.api.routes import channels
    from app.services.version_pins import open_pinned_dialog

    bot = _get_bot_or_404(db, bot_id, current_user)
    _persistent_license(db, current_user)
    _persistent_execution(bot.id)
    if payload.room_id is not None:
        binding = _persistent_binding(db, bot, current_user, payload.room_id)
        _persistent_runtime_service(
            db, _persistent_scope(binding), read_only=True
        )._binding(db, _persistent_scope(binding), read_only=True)
        return success_response(
            request, {"room": _persistent_room_payload(db, binding)}
        )
    version = db.scalar(
        select(BotVersion)
        .where(
            BotVersion.id == bot.active_version_id,
            BotVersion.bot_id == bot.id,
            BotVersion.status == "active",
            BotVersion.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if version is None:
        raise HTTPException(
            status_code=409, detail="운영 가능한 봇 버전을 먼저 적용해 주세요."
        )
    snapshot = _persistent_snapshot(db, bot, version, payload.dialog_id)
    state = channels._initial_runtime_state_for_version(bot, version, "webchat")
    room = ChannelRoom(
        channel_type="webchat",
        client_room_id="persistent-" + uuid4().hex,
        bot_id=bot.id,
        bot_version_id=version.id,
        participant_id=str(current_user.id),
        participant_name=current_user.name,
        status="open",
        metadata_json=state,
    )
    db.add(room)
    db.flush()
    scope = TurnScope(
        bot.organization_id,
        bot.group_id,
        bot.id,
        room.id,
        "user:" + str(current_user.id),
        1,
        version.id,
    )
    binding = open_pinned_dialog(db, scope, snapshot)
    room.metadata_json = {
        **state,
        "persistent_llm": {"owner_id": str(current_user.id), "generation": 1},
    }
    db.commit()
    return success_response(request, {"room": _persistent_room_payload(db, binding)})


@router.get("/{bot_id}/persistent/rooms/{room_id}")
def get_persistent_room(
    bot_id: UUID,
    room_id: UUID,
    request: Request,
    generation: int | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot_or_404(db, bot_id, current_user)
    _persistent_license(db, current_user)
    binding = _persistent_binding(db, bot, current_user, room_id, generation)
    runtime = _persistent_runtime_service(
        db, _persistent_scope(binding), read_only=True
    )
    runtime._binding(db, _persistent_scope(binding), read_only=True)
    messages = []
    for message in db.scalars(
        select(ChannelMessage)
        .where(ChannelMessage.room_id == room_id, ChannelMessage.deleted_at.is_(None))
        .order_by(ChannelMessage.created_at.asc())
    ).all():
        mark = (message.payload_json or {}).get("persistent_llm", {})
        if mark.get("binding_id") == str(binding.id):
            messages.append(
                {
                    "id": str(message.id),
                    "sender": "user" if message.participant_kind == "user" else "bot",
                    "text": message.text,
                    "memory_committed": mark.get("canonical") is True,
                }
            )
    return success_response(
        request, {"room": _persistent_room_payload(db, binding), "messages": messages}
    )


@router.post("/{bot_id}/persistent/rooms/{room_id}/turns", status_code=202)
def submit_persistent_turn(
    bot_id: UUID,
    room_id: UUID,
    payload: PersistentTurnRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot_or_404(db, bot_id, current_user)
    _persistent_license(db, current_user)
    _persistent_execution(bot.id)
    binding = _persistent_binding(db, bot, current_user, room_id)
    if binding.generation != payload.expected_generation:
        raise HTTPException(
            status_code=409, detail="대화가 변경되었습니다. 새 상태를 확인해 주세요."
        )
    scope = _persistent_scope(binding)
    runtime = _persistent_runtime_service(db, scope)
    profile = binding.snapshot_json.get("profiles", {}).get(payload.dialog_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="고정 버전의 모듈을 선택해 주세요.")
    try:
        if not profile["uses_llm"]:
            runtime._binding(db, scope)
            text = _persistent_render(
                db, binding, payload.dialog_id, "", payload.question
            )
            result = runtime.admit_fixed_turn(
                db,
                scope,
                payload.message_key,
                payload.question,
                server_text=text,
                dialog_id=payload.dialog_id,
            )
        else:
            context = {"cga_dialog_id": payload.dialog_id, "references": []}
            materialized = runtime.materialize_turn(
                db,
                scope,
                payload.question,
                turn_context=context,
                output_tokens=binding.snapshot_json["rate_policy"]["max_output_tokens"],
            )
            accounts = _persistent_accounts(db, binding)
            policy = binding.snapshot_json["rate_policy"]
            quote = BudgetQuote(
                accounts,
                policy["currency"],
                policy["tariff_version"],
                policy["amount_upper"],
                policy["context_input_tokens"],
                policy["max_output_tokens"],
                payload_hash(materialized),
            )
            result = runtime.admit_turn(
                db,
                scope,
                payload.message_key,
                payload.question,
                turn_context=context,
                quote=quote,
            )
        return success_response(request, result)
    except (TurnConflict, BudgetConflict, PinConflict) as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error


def _persistent_event_for_user(db, bot, user, room_id, event_id):
    event = db.get(ChannelQueueEvent, event_id)
    if (
        event is None
        or event.bot_id != bot.id
        or event.room_id != room_id
        or event.operation_kind != "persistent_llm"
    ):
        raise HTTPException(status_code=404, detail="대화 요청을 찾을 수 없습니다.")
    binding = db.get(ChannelLlmBinding, event.binding_id)
    if (
        binding is None
        or binding.user_id != user.id
        or binding.organization_id != user.organization_id
        or binding.group_id != user.group_id
    ):
        raise HTTPException(status_code=404, detail="대화 요청을 찾을 수 없습니다.")
    return binding, event


@router.get("/{bot_id}/persistent/rooms/{room_id}/turns/{event_id}")
def get_persistent_turn(
    bot_id: UUID,
    room_id: UUID,
    event_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot_or_404(db, bot_id, current_user)
    _persistent_license(db, current_user)
    binding, event = _persistent_event_for_user(
        db, bot, current_user, room_id, event_id
    )
    try:
        result = _persistent_runtime_service(
            db, _persistent_scope(binding), read_only=True
        ).view_turn(db, _persistent_scope(binding), event.id)
    except TurnConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return success_response(request, result)


@router.post(
    "/{bot_id}/persistent/rooms/{room_id}/turns/{event_id}/ack", status_code=202
)
def ack_persistent_turn(
    bot_id: UUID,
    room_id: UUID,
    event_id: UUID,
    payload: PersistentAckRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot_or_404(db, bot_id, current_user)
    _persistent_license(db, current_user)
    _persistent_execution(bot.id)
    binding, event = _persistent_event_for_user(
        db, bot, current_user, room_id, event_id
    )
    try:
        result = _persistent_runtime_service(
            db, _persistent_scope(binding), read_only=True
        ).ack_display(
            db,
            _persistent_scope(binding),
            event.id,
            message_id=payload.message_id,
            content_hash=payload.content_hash,
        )
    except TurnConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return success_response(request, result)


def _process_persistent_llm_events(db, request, *, limit=10):
    if (
        not settings.persistent_llm_enabled
        or not settings.persistent_llm_execution_enabled
    ):
        return []
    try:
        target = UUID(settings.persistent_llm_sample_bot_id)
    except ValueError:
        return []
    events = db.scalars(
        select(ChannelQueueEvent)
        .where(
            ChannelQueueEvent.bot_id == target,
            ChannelQueueEvent.operation_kind == "persistent_llm",
            ChannelQueueEvent.deleted_at.is_(None),
            ChannelQueueEvent.phase.in_(("reserved", "delivery_confirmed")),
        )
        .order_by(ChannelQueueEvent.created_at.asc())
        .limit(limit)
    ).all()
    results = []
    for candidate in events:
        identifier = candidate.id
        binding = db.get(ChannelLlmBinding, candidate.binding_id)
        if binding is None:
            continue
        scope = _persistent_scope(binding)
        try:
            runtime = _persistent_runtime_service(db, scope, event_id=identifier)
            result = (
                runtime.process_turn(db, scope, identifier)
                if candidate.phase == "reserved"
                else runtime.sync_turn(db, scope, identifier)
            )
        except Exception:
            db.rollback()
            event = db.get(ChannelQueueEvent, identifier, populate_existing=True)
            if event is None or event.phase == "committed":
                continue
            reservations = db.scalars(
                select(LlmBudgetReservation).where(
                    LlmBudgetReservation.event_id == identifier
                )
            ).all()
            if (
                event.submitted_at is None
                and reservations
                and all(row.state == "reserved" for row in reservations)
            ):
                release_presubmit_budget(db, identifier)
                event.phase = "access_blocked"
                event.status = "failed"
            else:
                if reservations and all(
                    row.state in {"reserved", "submitted", "unknown"}
                    for row in reservations
                ):
                    hold_unknown_budget(db, identifier)
                event.phase = "unknown"
                event.status = "unknown"
            event.error_message = "대화 실행 권한·설정 또는 결과 확인이 필요합니다. 자동 재생성하지 않습니다."
            db.commit()
            result = {
                "event_id": str(event.id),
                "phase": event.phase,
                "status": event.status,
            }
        results.append(result)
    return results
