from __future__ import annotations

from collections import defaultdict
import base64
import binascii
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.routes.channels import _process_queued_channel_events
from app.core.cache import cache_aside_json, cache_status_snapshot, purge_cache_pattern
from app.core.config import ROOT_DIR, settings
from app.core.responses import success_response
from app.core.language import normalize_supported_language
from app.core.version_documents import build_version_asset_counts, normalize_version_document
from app.core.version_storage import (
    build_version_dialog_asset_rows,
    build_version_dialog_flow_graph_rows,
    sync_version_dialog_split_tables,
)
from app.models import AdminChannel, AdminDefaultMessage, AdminLicense, AdminTemplate, AuditLog, Bot, BotVersion, ChannelMessage, ChannelQueueEvent, ChannelRoom, CommonVariable, EditLock, Group, Organization, Role, SignupRequest, User, UserRole, VersionDialogAsset, VersionDialogFlowGraph
from app.services.license_policy import assert_license_allows_creation, get_license_usage_counts, get_license_warnings
from app.services.nlu.deep_learning_lite import get_ml_acceleration_status
from app.services.scenario_validation import scenario_validation_from_version
from app.services.default_message_catalog import DEFAULT_MESSAGE_CATALOGS
from app.schemas.admin import (
    AdminApiCallHistoryItem,
    AdminCachePurgeRequest,
    AdminChannelItem,
    AdminCommonVariableItem,
    AdminDefaultMessageItem,
    AdminConversationHistoryItem,
    AdminGroupDetail,
    AdminGroupOption,
    AdminGroupListItem,
    AdminIntentFeedbackItem,
    AdminLicenseCurrent,
    AdminLicenseStatusResponse,
    AdminLicenseUsageItem,
    AdminLoginHistoryItem,
    AdminQueueHistoryItem,
    AdminTemplateItem,
    AdminTrainingHistoryItem,
    AdminUserDetail,
    AdminUserListItem,
    AdminVersionStorageBackfillRequest,
    ChannelCreateRequest,
    ChannelUpdateRequest,
    CommonVariableCreateRequest,
    CommonVariableImportRequest,
    CommonVariableUpdateRequest,
    DefaultMessageUpdateRequest,
    GroupCreateRequest,
    GroupUpdateRequest,
    LicenseApplyRequest,
    SignupApprovalRequest,
    UserGroupUpdateRequest,
    UserInfoUpdateRequest,
    UserRoleUpdateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
    UserStatusUpdateRequest,
)


router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_ROLE_CODES = {"system_manager", "it_admin"}
OPERATIONS_VIEW_ROLE_CODES = ADMIN_ROLE_CODES | {"operation_manager"}
LOGIN_HISTORY_ACTIONS = {"auth.login", "auth.logout"}

# 컨테이너 배포에서 앱이 놓이는 절대 경로. 소스 트리 기준인 ROOT_DIR와 별개라서
# 상수로 분리해 둔다. 이 값이 함수 안에 박혀 있으면 ROOT_DIR만 바꾸는 테스트가
# 호스트의 실제 로그를 함께 읽어 격리가 깨진다.
CONTAINER_ROOT_DIR = Path("/workspace")

_operations_dashboard_cache_lock = Lock()
_operations_dashboard_cache: dict[str, tuple[float, dict[str, object]]] = {}


def _operations_dashboard_cache_key(
    organization_id: UUID,
    *,
    days: int,
    hours: int | None,
    group_id: UUID | None,
    bot_id: UUID | None,
    channel_code: str | None,
) -> str:
    return ":".join(
        [
            str(organization_id),
            str(days),
            str(hours or ""),
            str(group_id or ""),
            str(bot_id or ""),
            str(channel_code or ""),
        ]
    )


def _get_operations_dashboard_cache(key: str) -> dict[str, object] | None:
    now = monotonic()
    with _operations_dashboard_cache_lock:
        cached = _operations_dashboard_cache.get(key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            _operations_dashboard_cache.pop(key, None)
            return None
        return payload


def _set_operations_dashboard_cache(key: str, payload: dict[str, object]) -> None:
    ttl_seconds = max(1, settings.operations_dashboard_cache_ttl_seconds)
    with _operations_dashboard_cache_lock:
        _operations_dashboard_cache[key] = (monotonic() + ttl_seconds, payload)
        if len(_operations_dashboard_cache) > 128:
            expired_keys = [
                cached_key
                for cached_key, (expires_at, _payload) in _operations_dashboard_cache.items()
                if expires_at <= monotonic()
            ]
            for expired_key in expired_keys:
                _operations_dashboard_cache.pop(expired_key, None)
            if len(_operations_dashboard_cache) > 128:
                oldest_key = min(
                    _operations_dashboard_cache,
                    key=lambda cached_key: _operations_dashboard_cache[cached_key][0],
                )
                _operations_dashboard_cache.pop(oldest_key, None)


def _purge_operations_dashboard_cache() -> None:
    with _operations_dashboard_cache_lock:
        _operations_dashboard_cache.clear()


ADMIN_HISTORY_DEFAULT_PAGE_SIZE = 20
ADMIN_HISTORY_MAX_PAGE_SIZE = 100
ADMIN_CONVERSATION_MESSAGES_PER_ROOM = 200
ADMIN_CONVERSATION_QUEUE_EVENTS_PER_ROOM = 100


def _vector_worker_acceleration_status() -> dict[str, object]:
    """Read optional worker telemetry without making the dashboard unavailable on failure."""
    base_url = settings.aidot_vector_worker_base_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return {"available": False, "error": "worker_url_invalid"}
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=1.5)
    try:
        path = f"{parsed.path.rstrip('/')}/health" if parsed.path.rstrip("/") else "/health"
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            return {"available": False, "error": f"worker_http_{response.status}"}
        payload = json.loads(body)
        acceleration = payload.get("acceleration") if isinstance(payload, dict) else None
        return acceleration if isinstance(acceleration, dict) else {"available": False, "error": "worker_acceleration_missing"}
    except (HTTPException, OSError, ValueError, json.JSONDecodeError):
        return {"available": False, "error": "worker_unreachable"}
    finally:
        connection.close()
SYSTEM_COMMON_VARIABLES = [
    {
        "name": "_bot_hub_id",
        "value": "",
        "description": "봇 허브 소속일 때의 허브 ID",
    },
    {
        "name": "_bot_hub_name",
        "value": "",
        "description": "봇 허브 소속일 때의 허브 이름",
    },
    {
        "name": "_bot_name",
        "value": "",
        "description": "현재 봇 이름",
    },
    {
        "name": "_channel_id",
        "value": "",
        "description": "메신저 채널 ID",
    },
    {
        "name": "_date_time",
        "value": "",
        "description": "현재 날짜시각",
    },
    {
        "name": "_msg",
        "value": "",
        "description": "직전 사용자 발화",
    },
    {
        "name": "_dialog_start_time",
        "value": "",
        "description": "현재 대화 시작시간",
    },
    {
        "name": "_id",
        "value": "",
        "description": "현재 대화 컨텍스트 ID",
    },
    {
        "name": "_session_id",
        "value": "",
        "description": "Session ID",
    },
    {
        "name": "_today",
        "value": "",
        "description": "오늘 날짜",
    },
    {
        "name": "_user_id",
        "value": "",
        "description": "현재 사용자 ID",
    },
    {
        "name": "_user_name",
        "value": "",
        "description": "현재 사용자 이름",
    },
    {
        "name": "_bot_id",
        "value": "",
        "description": "현재 봇 ID",
    },
    {
        "name": "_dialog_id",
        "value": "",
        "description": "현재 의도 Key",
    },
    {
        "name": "_semantic_answers",
        "value": "",
        "description": "Semantic RAG 답변 후보 목록",
    },
    {
        "name": "_semantic_answer_text",
        "value": "",
        "description": "Semantic RAG 최상위 답변 본문",
    },
    {
        "name": "_semantic_answer_score",
        "value": "",
        "description": "Semantic RAG 최상위 답변 Score",
    },
    {
        "name": "_semantic_answer_intent_id",
        "value": "",
        "description": "Semantic RAG 답변 의도 ID",
    },
    {
        "name": "_semantic_answer_intent_name",
        "value": "",
        "description": "Semantic RAG 답변 의도명",
    },
    {
        "name": "_semantic_answer_source_type",
        "value": "",
        "description": "Semantic RAG 답변 출처 유형",
    },
    {
        "name": "_semantic_answer_source_title",
        "value": "",
        "description": "Semantic RAG 답변 출처 제목",
    },
    {
        "name": "_semantic_answer_page",
        "value": "",
        "description": "Semantic RAG PDF 페이지",
    },
    {
        "name": "_rag_answers",
        "value": "",
        "description": "Semantic RAG 답변 후보 목록(호환)",
    },
    {
        "name": "_rag_answer_text",
        "value": "",
        "description": "Semantic RAG 최상위 답변 본문(호환)",
    },
    {
        "name": "_rag_answer_score",
        "value": "",
        "description": "Semantic RAG 최상위 답변 Score(호환)",
    },
    {
        "name": "_rag_answer_intent_id",
        "value": "",
        "description": "Semantic RAG 답변 의도 ID(호환)",
    },
    {
        "name": "_rag_answer_intent_name",
        "value": "",
        "description": "Semantic RAG 답변 의도명(호환)",
    },
    {
        "name": "_rag_answer_source_type",
        "value": "",
        "description": "Semantic RAG 답변 출처 유형(호환)",
    },
    {
        "name": "_rag_answer_source_title",
        "value": "",
        "description": "Semantic RAG 답변 출처 제목(호환)",
    },
    {
        "name": "_rag_answer_page",
        "value": "",
        "description": "Semantic RAG PDF 페이지(호환)",
    },
    {
        "name": "_llm_answers",
        "value": "",
        "description": "LLM RAG 답변 후보 목록",
    },
    {
        "name": "_llm_answer_text",
        "value": "",
        "description": "LLM RAG 최상위 답변 본문",
    },
    {
        "name": "_llm_answer_score",
        "value": "",
        "description": "LLM RAG 최상위 답변 Score",
    },
    {
        "name": "_llm_answer_intent_id",
        "value": "",
        "description": "LLM RAG 답변 의도 ID",
    },
    {
        "name": "_llm_answer_intent_name",
        "value": "",
        "description": "LLM RAG 답변 의도명",
    },
    {
        "name": "_llm_answer_source_type",
        "value": "",
        "description": "LLM RAG 답변 출처 유형",
    },
    {
        "name": "_llm_answer_source_title",
        "value": "",
        "description": "LLM RAG 답변 출처 제목",
    },
    {
        "name": "_llm_answer_page",
        "value": "",
        "description": "LLM RAG PDF 페이지",
    },
]
LEGACY_SYSTEM_COMMON_VARIABLE_NAMES = {
    "botName": "_botName",
    "_bitId": "_botId",
    "_botName": "_bot_name",
    "_botId": "_bot_id",
    "_dialogID": "_dialog_id",
    "_userId": "_user_id",
    "_userName": "_user_name",
}
DEFAULT_ADMIN_CHANNELS = [
    {
        "code": "SM_CHAT",
        "name": "Simulator",
        "provider": "simulator",
        "renderer_type": "simulator",
        "endpoint_url": None,
        "auth_type": "none",
        "description": "시뮬레이터 채널",
    },
    {
        "code": "WEBCHAT",
        "name": "Webchat",
        "provider": "webchat",
        "renderer_type": "webchat",
        "endpoint_url": None,
        "auth_type": "none",
        "description": "웹 채팅 채널",
    },
    {
        "code": "KAKAO",
        "name": "Kakao",
        "provider": "kakao",
        "renderer_type": "kakao",
        "endpoint_url": "/api/v1/channels/kakao/webhook",
        "auth_type": "none",
        "description": "카카오 채널",
    },
    {
        "code": "TEAMS",
        "name": "MS Teams",
        "provider": "ms_teams",
        "renderer_type": "adaptive_card",
        "endpoint_url": None,
        "auth_type": "oauth",
        "description": "Microsoft Teams 채널",
    },
]
DEFAULT_ADMIN_TEMPLATES = [
    {
        "channel_code": "SM_CHAT",
        "name": "기본 메시지",
        "renderer_type": "text",
        "item_types": "text",
        "description": "기본 텍스트 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Html",
        "renderer_type": "html",
        "item_types": "html",
        "description": "HTML 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Card",
        "renderer_type": "card",
        "item_types": "title, imageUrl, description",
        "description": "카드형 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Table",
        "renderer_type": "table",
        "item_types": "table",
        "description": "테이블 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Button",
        "renderer_type": "button",
        "item_types": "button",
        "description": "버튼 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Link Button",
        "renderer_type": "link-button",
        "item_types": "label, url",
        "description": "링크 버튼 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Form(Rich)",
        "renderer_type": "form",
        "item_types": "formMessage",
        "description": "Rich Form 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Carousel",
        "renderer_type": "carousel",
        "item_types": "carousel",
        "description": "캐러셀 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "DTMF",
        "renderer_type": "dtmf",
        "item_types": "shortText, stepper, stepper",
        "description": "Voice Bot DTMF 메시지",
    },
    {
        "channel_code": "SM_CHAT",
        "name": "Form(A Card)",
        "renderer_type": "form-a-card",
        "item_types": "adaptiveCard",
        "description": "Adaptive Cards 메시지",
    },
    {
        "channel_code": "KAKAO",
        "name": "기본 메시지",
        "renderer_type": "simple-text",
        "item_types": "text",
        "description": "카카오 기본 텍스트 메시지",
    },
    {
        "channel_code": "KAKAO",
        "name": "Quick Replies",
        "renderer_type": "quick-reply",
        "item_types": "text, quickReplies",
        "description": "카카오 빠른 답장 메시지",
    },
    {
        "channel_code": "KAKAO",
        "name": "Basic Card",
        "renderer_type": "basic-card",
        "item_types": "title, description, imageUrl, buttons",
        "description": "카카오 기본 카드 메시지",
    },
    {
        "channel_code": "KAKAO",
        "name": "List Card",
        "renderer_type": "list-card",
        "item_types": "header, items",
        "description": "카카오 목록형 카드 메시지",
    },
    {
        "channel_code": "KAKAO",
        "name": "Carousel",
        "renderer_type": "carousel",
        "item_types": "title, imageUrl, items, buttons",
        "description": "카카오 기본 카드형 캐러셀 메시지",
    },
]
DEFAULT_ADMIN_DEFAULT_MESSAGES = [
    {
        "message_key": "intent_fallback",
        "message_name": "의도 미분류 메시지",
        "category": "intent",
        "message_text": "질문을 이해하지 못했습니다. 다시 말씀해주세요.",
        "description": "사용자 발화에서 의도를 찾지 못했을 때 출력합니다.",
    },
    {
        "message_key": "multi_intent_guide",
        "message_name": "다중 의도 선택 안내",
        "category": "intent",
        "message_text": "아래 후보 중 원하는 의도를 선택해주세요.",
        "description": "여러 의도가 후보로 잡혔을 때 출력합니다.",
    },
    {
        "message_key": "no_desired_intent",
        "message_name": "원하는 의도 없음 메시지",
        "category": "intent",
        "message_text": "원하는 의도가 없습니다. 다시 말씀해주세요.",
        "description": "사용자가 후보 의도 중 원하는 의도가 없다고 선택했을 때 출력합니다.",
    },
    {
        "message_key": "intent_receipt",
        "message_name": "의도 접수 메시지",
        "category": "intent",
        "message_text": "{{intentName}} 의도로 접수되었습니다.",
        "description": "연결된 대화 흐름이 없고 의도만 인식되었을 때 출력합니다.",
    },
    {
        "message_key": "invalid_button",
        "message_name": "버튼 오류 메시지",
        "category": "input",
        "message_text": "선택할 수 없는 항목입니다. 다시 선택해주세요.",
        "description": "유효하지 않은 버튼이나 선택지가 입력되었을 때 출력합니다.",
    },
    {
        "message_key": "generic_select",
        "message_name": "기본 선택 안내",
        "category": "input",
        "message_text": "선택하세요.",
        "description": "버튼형 메시지에 안내 문구가 없을 때 출력합니다.",
    },
    {
        "message_key": "table_select",
        "message_name": "테이블 선택 안내",
        "category": "input",
        "message_text": "아래 중 선택하세요.",
        "description": "테이블형 메시지에 안내 문구가 없을 때 출력합니다.",
    },
    {
        "message_key": "system_error",
        "message_name": "시스템 오류 메시지",
        "category": "error",
        "message_text": "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        "description": "API 또는 시스템 오류가 발생했을 때 출력합니다.",
    },
    {
        "message_key": "runtime_flow_error",
        "message_name": "대화 흐름 설정 오류 메시지",
        "category": "error",
        "message_text": "대화 흐름 설정 오류로 대화를 계속할 수 없습니다.",
        "description": "Condition, 연결선, 실행 카드 등 대화 흐름 설정 오류가 발생했을 때 출력합니다.",
    },
    {
        "message_key": "runtime_module_not_found",
        "message_name": "대화 모듈 연결 오류 메시지",
        "category": "error",
        "message_text": "연결할 대화 모듈을 찾지 못했습니다.",
        "description": "Jump 카드가 연결할 대화 모듈을 찾지 못했을 때 출력합니다.",
    },
    {
        "message_key": "runtime_flow_limit",
        "message_name": "대화 흐름 실행 한도 초과 메시지",
        "category": "error",
        "message_text": "대화 흐름 실행 한도를 초과했습니다.",
        "description": "대화 흐름이 비정상적으로 반복되어 실행 한도를 초과했을 때 출력합니다.",
    },
    {
        "message_key": "timeout",
        "message_name": "타임아웃 메시지",
        "category": "session",
        "message_text": "응답 시간이 초과되었습니다. 처음부터 다시 진행해주세요.",
        "description": "대화 타임아웃 발생 시 출력합니다.",
    },
    {
        "message_key": "session_end",
        "message_name": "세션 종료 메시지",
        "category": "session",
        "message_text": "대화가 종료되었습니다.",
        "description": "세션 종료 시 출력합니다.",
    },
    {
        "message_key": "conversation_in_progress",
        "message_name": "진행 중 대화 안내",
        "category": "session",
        "message_text": "진행 중인 대화가 있습니다. 현재 대화를 먼저 완료해주세요.",
        "description": "이미 진행 중인 대화가 있을 때 출력합니다.",
    },
]
DEFAULT_ADMIN_DEFAULT_MESSAGE_BY_KEY = {item["message_key"]: item for item in DEFAULT_ADMIN_DEFAULT_MESSAGES}

def _user_account_status_label(status_value: str) -> str:
    return {
        "active": "활성",
        "inactive": "비활성",
        "locked": "잠김",
        "password_reset": "비밀번호 초기화",
    }.get(status_value, status_value)


def _signup_status_label(status_value: str) -> str:
    return {
        "pending": "승인 요청",
        "rejected": "승인 반려",
        "approved": "계정 승인",
        "deleted": "계정 삭제",
    }.get(status_value, status_value)


def _get_default_organization(db: Session) -> Organization:
    organization = db.scalar(
        select(Organization).where(
            Organization.code == "default",
            Organization.deleted_at.is_(None),
        )
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="기본 서버가 설정되지 않았습니다.",
        )
    return organization


def _get_user_role_codes(db: Session, user_id: UUID) -> list[str]:
    rows = db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).all()
    return [row[0] for row in rows]


def _get_user_role_map(db: Session, user_ids: list[UUID]) -> dict[UUID, Role]:
    if not user_ids:
        return {}

    rows = db.execute(
        select(UserRole.user_id, Role)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
    ).all()

    role_map: dict[UUID, Role] = {}
    for user_id, role in rows:
        role_map[user_id] = role
    return role_map


def _get_role_by_code(db: Session, role_code: str) -> Role:
    role = db.scalar(select(Role).where(Role.code == role_code, Role.deleted_at.is_(None)))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="선택한 역할이 존재하지 않습니다.",
        )
    return role


def _get_group_name_map(db: Session, group_ids: list[UUID]) -> dict[UUID, Group]:
    if not group_ids:
        return {}
    groups = db.execute(
        select(Group).where(Group.id.in_(group_ids), Group.deleted_at.is_(None))
    ).scalars().all()
    return {group.id: group for group in groups}


def _get_available_group_options(db: Session, organization_id: UUID) -> list[AdminGroupOption]:
    groups = db.execute(
        select(Group)
        .where(
            Group.organization_id == organization_id,
            Group.deleted_at.is_(None),
        )
        .order_by(Group.created_at.asc())
    ).scalars().all()
    return [
        AdminGroupOption(id=group.id, code=group.code, name=group.name)
        for group in groups
    ]


def _get_group_or_404(db: Session, group_id: UUID) -> Group:
    group = db.scalar(select(Group).where(Group.id == group_id, Group.deleted_at.is_(None)))
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="그룹을 찾을 수 없습니다.",
        )
    return group


def _build_group_json(group: Group, organization: Organization | None = None) -> dict[str, object]:
    return {
        "id": str(group.id),
        "code": group.code,
        "name": group.name,
        "status": group.status,
        "organization_id": str(group.organization_id),
        "organization_name": organization.name if organization is not None else None,
    }


def _build_user_json(
    user: User,
    role_codes: list[str],
    group: Group | None = None,
    organization: Organization | None = None,
) -> dict[str, object]:
    return {
        "id": str(user.id),
        "login_id": user.login_id,
        "name": user.name,
        "email": user.email,
        "status": user.status,
        "group_id": str(user.group_id) if user.group_id else None,
        "group_name": group.name if group is not None else None,
        "organization_id": str(user.organization_id),
        "organization_name": organization.name if organization is not None else None,
        "roles": role_codes,
        "is_protected": user.is_protected,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _build_signup_request_json(
    signup_request: SignupRequest,
    group: Group | None = None,
    organization: Organization | None = None,
) -> dict[str, object]:
    return {
        "id": str(signup_request.id),
        "login_id": signup_request.login_id,
        "name": signup_request.name,
        "comment": signup_request.comment,
        "preferred_language": signup_request.preferred_language,
        "status": signup_request.status,
        "requested_role_code": signup_request.requested_role_code,
        "group_id": str(signup_request.group_id),
        "group_name": group.name if group is not None else None,
        "organization_id": str(signup_request.organization_id),
        "organization_name": organization.name if organization is not None else None,
    }


def _build_common_variable_json(variable: CommonVariable, updater_name: str | None = None) -> dict[str, object]:
    return {
        "id": str(variable.id),
        "kind": variable.kind,
        "name": variable.name,
        "value": variable.value,
        "description": variable.description,
        "organization_id": str(variable.organization_id),
        "updated_by": str(variable.updated_by) if variable.updated_by else None,
        "updater_name": updater_name,
        "protected": True,
    }


def _build_channel_config(
    provider: str,
    renderer_type: str,
    endpoint_url: str | None,
    auth_type: str,
    auth_config: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_provider = provider.strip().lower()
    normalized_endpoint = endpoint_url.strip() if endpoint_url else None
    if not normalized_endpoint and normalized_provider == "kakao":
        normalized_endpoint = "/api/v1/channels/kakao/webhook"
    return {
        "provider": normalized_provider,
        "renderer_type": renderer_type.strip().lower(),
        "endpoint_url": normalized_endpoint,
        "auth_type": auth_type.strip().lower() if auth_type else "none",
        "auth_config": auth_config or {},
    }


def _build_channel_json(
    channel: AdminChannel,
    creator_name: str | None = None,
    updater_name: str | None = None,
) -> dict[str, object]:
    current = dict(channel.data_json or {})
    current.update(
        {
            "id": str(channel.id),
            "code": channel.code,
            "name": channel.name,
            "description": channel.description,
            "status": channel.status,
            "status_label": "사용" if channel.status == "active" else "미사용",
            "organization_id": str(channel.organization_id),
            "created_by": str(channel.created_by) if channel.created_by else None,
            "updated_by": str(channel.updated_by) if channel.updated_by else None,
            "creator_name": creator_name,
            "updater_name": updater_name,
            "protected": True,
        }
    )
    current.setdefault("provider", "webchat")
    current.setdefault("renderer_type", "webchat")
    current.setdefault("endpoint_url", None)
    current.setdefault("auth_type", "none")
    current.setdefault("auth_config", {})
    return current


def _channel_auth_token_value(auth_config: dict[str, object]) -> str:
    for key in ("token", "channelToken", "accessToken", "secret", "appSecret"):
        value = auth_config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _channel_connection_issues(
    *,
    status_value: str,
    provider: str,
    renderer_type: str,
    endpoint_url: str | None,
    auth_type: str,
    auth_config: dict[str, object],
) -> list[str]:
    issues: list[str] = []
    normalized_provider = provider.strip().lower()
    normalized_renderer = renderer_type.strip().lower()
    normalized_auth_type = auth_type.strip().lower() if auth_type else "none"
    normalized_endpoint = endpoint_url.strip() if isinstance(endpoint_url, str) and endpoint_url.strip() else ""

    if status_value != "active":
        issues.append("채널이 미사용 상태입니다.")
    if normalized_provider in {"kakao", "ms_teams"} and not normalized_endpoint:
        issues.append("외부 채널은 Endpoint URL 설정이 필요합니다.")
    if normalized_auth_type != "none" and not auth_config:
        issues.append("인증 방식이 설정되어 있으나 인증 정보가 비어 있습니다.")

    if normalized_provider == "kakao":
        if normalized_renderer != "kakao":
            issues.append("카카오 채널은 renderer_type이 kakao여야 합니다.")
        if normalized_auth_type not in {"none", "token"}:
            issues.append("카카오 채널은 auth_type으로 none 또는 token만 지원합니다.")
        if normalized_endpoint and not normalized_endpoint.endswith("/api/v1/channels/kakao/webhook"):
            issues.append("카카오 채널 Endpoint URL은 /api/v1/channels/kakao/webhook 형식이어야 합니다.")
        if normalized_auth_type == "token" and not _channel_auth_token_value(auth_config):
            issues.append("카카오 token 인증은 auth_config에 token 또는 appSecret 값이 필요합니다.")

    return issues


def _template_renderer_issues(
    *,
    channel_code: str,
    renderer_type: str,
) -> list[str]:
    normalized_channel_code = channel_code.strip().upper()
    normalized_renderer_type = renderer_type.strip().lower()

    if normalized_channel_code == "KAKAO":
        if normalized_renderer_type not in {"simple-text", "quick-reply", "basic-card", "list-card", "carousel"}:
            return ["카카오 템플릿은 simple-text, quick-reply, basic-card, list-card, carousel만 지원합니다."]
        return []

    if normalized_channel_code in {"SLACK", "DISCORD", "TELEGRAM", "GOOGLE_CHAT", "LINE"}:
        if normalized_renderer_type not in {"text", "button", "card", "table", "link-button", "carousel"}:
            return [f"{'LINE' if normalized_channel_code == 'LINE' else 'Google Chat' if normalized_channel_code == 'GOOGLE_CHAT' else 'Telegram' if normalized_channel_code == 'TELEGRAM' else 'Discord' if normalized_channel_code == 'DISCORD' else 'Slack'} 템플릿은 text, button, card, table, link-button, carousel만 지원합니다."]
        return []

    if normalized_renderer_type in {"simple-text", "quick-reply", "basic-card", "list-card"}:
        return ["카카오 전용 템플릿은 KAKAO 채널에서만 사용할 수 있습니다."]

    return []


def _build_template_json(
    template: AdminTemplate,
    channel_name: str | None = None,
    creator_name: str | None = None,
    updater_name: str | None = None,
) -> dict[str, object]:
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
        "organization_id": str(template.organization_id),
        "created_by": str(template.created_by) if template.created_by else None,
        "updated_by": str(template.updated_by) if template.updated_by else None,
        "creator_name": creator_name,
        "updater_name": updater_name,
        "protected": True,
    }


def _require_admin_user(
    db: Session,
    current_user: User,
) -> set[str]:
    role_codes = set(_get_user_role_codes(db, current_user.id))
    if not role_codes.intersection(ADMIN_ROLE_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
    return role_codes


def _require_operations_view_user(
    db: Session,
    current_user: User,
) -> set[str]:
    role_codes = set(_get_user_role_codes(db, current_user.id))
    if not role_codes.intersection(OPERATIONS_VIEW_ROLE_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="운영 현황 조회 권한이 필요합니다.",
        )
    return role_codes


def _ensure_assignable_role(current_role_codes: set[str], requested_role_code: str) -> None:
    if requested_role_code == "it_admin" and "it_admin" not in current_role_codes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="IT 관리자 역할은 IT 관리자만 부여할 수 있습니다.",
        )


LICENSE_FORMAT = "cga-license"
LICENSE_SIGNATURE_ALGORITHM = "RS256"
LICENSE_USAGE_DEFINITIONS = [
    ("users", "사용자"),
    ("bots", "봇"),
    ("apis", "API"),
]


def _canonical_license_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _license_public_key_pem() -> str:
    raw_key = settings.cga_license_public_key.strip()
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이선스 검증 public key가 설정되지 않았습니다.",
        )
    normalized = raw_key.replace("\\n", "\n")
    if "BEGIN PUBLIC KEY" in normalized:
        return normalized
    try:
        decoded = base64.b64decode(normalized, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return normalized
    return decoded.replace("\\n", "\n")


def _decode_license_signature(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 서명 형식이 올바르지 않습니다.") from exc


def _parse_license_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 날짜 형식이 올바르지 않습니다.") from exc


def _verify_license_text(license_text: str) -> dict[str, object]:
    try:
        license_doc = json.loads(license_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 파일이 JSON 형식이 아닙니다.") from exc
    if not isinstance(license_doc, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 파일 구조가 올바르지 않습니다.")
    if license_doc.get("format") != LICENSE_FORMAT:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="지원하지 않는 라이선스 형식입니다.")

    payload = license_doc.get("payload")
    signature = license_doc.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 payload 또는 signature가 없습니다.")
    if signature.get("algorithm") != LICENSE_SIGNATURE_ALGORITHM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="지원하지 않는 라이선스 서명 알고리즘입니다.")

    signature_value = signature.get("value")
    if not isinstance(signature_value, str) or not signature_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 서명 값이 없습니다.")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="라이선스 검증 모듈이 설치되지 않았습니다. cryptography 패키지를 설치해주세요.",
        ) from exc

    public_key = serialization.load_pem_public_key(_license_public_key_pem().encode("utf-8"))
    try:
        public_key.verify(
            _decode_license_signature(signature_value),
            _canonical_license_payload(payload),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 서명 검증에 실패했습니다.") from exc

    license_id = payload.get("license_id")
    product = payload.get("product")
    customer = payload.get("customer")
    limits = payload.get("limits")
    if not isinstance(license_id, str) or not license_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 ID가 없습니다.")
    if product != "CGA":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CGA 제품 라이선스가 아닙니다.")
    if not isinstance(customer, dict) or not isinstance(customer.get("name"), str) or not customer.get("name"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 고객 정보가 없습니다.")
    if not isinstance(limits, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="라이선스 제한 정보가 없습니다.")
    for key, _label in LICENSE_USAGE_DEFINITIONS:
        limit = limits.get(key)
        if not isinstance(limit, int) or limit < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"라이선스 {key} 제한 값이 올바르지 않습니다.")

    expires_at = _parse_license_date(payload.get("expires_at"))
    if expires_at is not None and expires_at < date.today():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="만료된 라이선스는 적용할 수 없습니다.")
    _parse_license_date(payload.get("issued_at"))
    return license_doc


def _license_api_asset_key(api_asset: dict[str, object]) -> str | None:
    for key in ("id", "apiId", "name", "baseUrl"):
        value = api_asset.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _iter_api_assets(value: object):
    if isinstance(value, dict):
        for key in ("apis", "api_assets", "apiAssets"):
            assets = value.get(key)
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict):
                        yield asset
        for child in value.values():
            yield from _iter_api_assets(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_api_assets(child)


def _count_registered_apis(db: Session, organization_id: UUID) -> int:
    versions = db.execute(
        select(BotVersion.version_json)
        .join(Bot, Bot.id == BotVersion.bot_id)
        .where(
            Bot.organization_id == organization_id,
            Bot.deleted_at.is_(None),
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    api_keys: set[str] = set()
    for (document,) in versions:
        for api_asset in _iter_api_assets(document or {}):
            key = _license_api_asset_key(api_asset)
            if key:
                api_keys.add(key)
    return len(api_keys)


def _license_usage_items(
    organization: Organization,
    payload: dict[str, object] | None,
    usage_counts: dict[str, int],
) -> list[AdminLicenseUsageItem]:
    limits = payload.get("limits", {}) if payload else {}
    expires_at = payload.get("expires_at") if payload else None
    items: list[AdminLicenseUsageItem] = []
    for key, label in LICENSE_USAGE_DEFINITIONS:
        raw_limit = limits.get(key) if isinstance(limits, dict) else None
        limit = raw_limit if isinstance(raw_limit, int) else None
        used = int(usage_counts[key])
        remaining = max(limit - used, 0) if limit is not None else None
        items.append(
            AdminLicenseUsageItem(
                key=key,
                label=label,
                limit=limit,
                used=used,
                remaining=remaining,
                expires_at=expires_at if isinstance(expires_at, str) else None,
            )
        )
    return items


def _current_admin_license(db: Session, organization_id: UUID) -> AdminLicense | None:
    return db.scalar(
        select(AdminLicense)
        .where(AdminLicense.organization_id == organization_id, AdminLicense.status == "active")
        .order_by(AdminLicense.updated_at.desc())
    )


def _license_effective_status(record: AdminLicense) -> str:
    expires_at = _parse_license_date((record.payload_json or {}).get("expires_at"))
    if expires_at is not None and expires_at < date.today():
        return "expired"
    return record.status


def _serialize_license_status(db: Session, organization: Organization, record: AdminLicense | None) -> AdminLicenseStatusResponse:
    usage_counts = get_license_usage_counts(db, organization.id)
    warnings = get_license_warnings(
        db,
        organization.id,
        license_record=record,
        usage_counts=usage_counts,
    )
    if record is None:
        return AdminLicenseStatusResponse(
            installed=False,
            message=" / ".join(warnings) if warnings else "적용된 라이선스가 없습니다.",
            usage=_license_usage_items(organization, None, usage_counts),
        )
    payload = record.payload_json or {}
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    customer_name = record.customer_name
    if customer and isinstance(customer.get("name"), str):
        customer_name = customer["name"]
    license_current = AdminLicenseCurrent(
        id=record.id,
        license_id=record.license_id,
        product=record.product,
        customer_name=customer_name,
        issued_at=payload.get("issued_at") if isinstance(payload.get("issued_at"), str) else record.issued_at_text,
        expires_at=payload.get("expires_at") if isinstance(payload.get("expires_at"), str) else record.expires_at_text,
        status=_license_effective_status(record),
        features=payload.get("features") if isinstance(payload.get("features"), dict) else {},
        binding=payload.get("binding") if isinstance(payload.get("binding"), dict) else {},
        updated_at=record.updated_at,
    )
    return AdminLicenseStatusResponse(
        installed=True,
        message=" / ".join(warnings) if warnings else "라이선스가 적용되어 있습니다.",
        license=license_current,
        usage=_license_usage_items(organization, payload, usage_counts),
    )

def _serialize_user_list_item(
    user: User,
    group_name: str,
    role: Role | None,
) -> AdminUserListItem:
    role_code = role.code if role is not None else "curator"
    role_name = role.name if role is not None else "큐레이터"
    return AdminUserListItem(
        id=user.id,
        kind="user",
        login_id=user.login_id,
        name=user.name,
        group_name=group_name,
        role_code=role_code,  # type: ignore[arg-type]
        role_name=role_name,
        requested_at=user.created_at,
        signup_status="계정 승인",
        account_status=_user_account_status_label(user.status),
        is_protected=user.is_protected,
    )


def _serialize_signup_list_item(
    signup_request: SignupRequest,
    group_name: str,
    role: Role | None,
) -> AdminUserListItem:
    role_code = signup_request.requested_role_code
    role_name = role.name if role is not None else signup_request.requested_role_code
    signup_status = _signup_status_label(signup_request.status)
    return AdminUserListItem(
        id=signup_request.id,
        kind="signup_request",
        login_id=signup_request.login_id,
        name=signup_request.name,
        group_name=group_name,
        role_code=role_code,  # type: ignore[arg-type]
        role_name=role_name,
        requested_at=signup_request.created_at,
        signup_status=signup_status,
        account_status="활성",
        is_protected=False,
    )


def _actor_name_map(db: Session, actor_ids: set[UUID]) -> dict[UUID, str]:
    if not actor_ids:
        return {}
    users = db.execute(select(User).where(User.id.in_(actor_ids))).scalars().all()
    return {user.id: user.login_id for user in users}


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_in_range(
    value: datetime,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> bool:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if start_at is not None and normalized < start_at:
        return False
    if end_at is not None and normalized > end_at:
        return False
    return True


def _read_bounded_log_tail(
    log_path: Path,
    *,
    max_bytes: int,
    max_lines: int,
) -> tuple[list[tuple[int, str]], bool]:
    byte_limit = max(1024, int(max_bytes))
    line_limit = max(1, int(max_lines))
    try:
        file_size = log_path.stat().st_size
        start_offset = max(0, file_size - byte_limit)
        with log_path.open("rb") as stream:
            if start_offset > 0:
                stream.seek(start_offset - 1)
                starts_at_line_boundary = stream.read(1) in {b"\n", b"\r"}
                stream.seek(start_offset)
                if not starts_at_line_boundary:
                    stream.readline(byte_limit)
                    start_offset = stream.tell()
            else:
                stream.seek(0)
            raw_content = stream.read(byte_limit)
    except OSError:
        return [], False

    lines: list[tuple[int, str]] = []
    current_offset = start_offset
    for raw_line in raw_content.splitlines(keepends=True):
        line_offset = current_offset
        current_offset += len(raw_line)
        line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        lines.append((line_offset + 1, line))

    truncated = start_offset > 0
    if len(lines) > line_limit:
        lines = lines[-line_limit:]
        truncated = True
    return lines, truncated


def _read_web_jsonl_logs(
    log_dir_name: str,
    prefix: str,
    max_files: int = 30,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_entries: int | None = None,
    max_bytes_per_file: int | None = None,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    entry_limit = max(1, int(max_entries or settings.admin_jsonl_scan_max_entries))
    byte_limit = max(1024, int(max_bytes_per_file or settings.admin_jsonl_scan_max_bytes_per_file))
    log_dirs = [
        ROOT_DIR / "apps" / "web" / "logs" / log_dir_name,
        ROOT_DIR / "logs" / log_dir_name,
        CONTAINER_ROOT_DIR / "logs" / log_dir_name,
    ]
    log_files: list[Path] = []
    seen_paths: set[Path] = set()
    for log_dir in log_dirs:
        if not log_dir.exists():
            continue
        for log_file in sorted(log_dir.glob(f"{prefix}-*.log"), key=lambda item: item.stat().st_mtime, reverse=True):
            resolved = log_file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            log_files.append(log_file)
    log_files = sorted(log_files, key=lambda item: item.stat().st_mtime, reverse=True)[:max_files]
    for log_file in log_files:
        log_lines, _ = _read_bounded_log_tail(
            log_file,
            max_bytes=byte_limit,
            max_lines=entry_limit,
        )
        file_entries: list[dict[str, object]] = []
        for _, line in reversed(log_lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if start_at is not None or end_at is not None:
                server_time = _parse_datetime(entry.get("serverTime"))
                if server_time is None:
                    continue
                if start_at is not None:
                    normalized_time = server_time if server_time.tzinfo is not None else server_time.replace(tzinfo=timezone.utc)
                    if normalized_time < start_at:
                        break
                if not _datetime_in_range(server_time, start_at=start_at, end_at=end_at):
                    continue
            file_entries.append(entry)
            if len(entries) + len(file_entries) >= entry_limit:
                break
        entries.extend(reversed(file_entries))
        if len(entries) >= entry_limit:
            break
    return entries


def _simulator_result_priority(value: object) -> int:
    result = str(value or "").strip()
    if not result or result == "진행중":
        return 0
    if "오류" in result or "미분류" in result or "미응답" in result:
        return 3
    if "유사" in result:
        return 2
    if "정상" in result:
        return 1
    return 1


def _bot_group_context_map(db: Session, bot_ids: set[UUID]) -> dict[UUID, tuple[str, str]]:
    if not bot_ids:
        return {}
    bots = db.execute(
        select(Bot).where(Bot.id.in_(bot_ids))
    ).scalars().all()
    group_map = _get_group_name_map(db, [bot.group_id for bot in bots if bot.group_id is not None])
    return {
        bot.id: (
            group_map.get(bot.group_id).name if bot.group_id in group_map else "-",
            bot.name,
        )
        for bot in bots
    }


def _version_no_from_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower().startswith("v") and stripped[1:].isdigit():
            return int(stripped[1:])
        if stripped.isdigit():
            return int(stripped)
    return 0


def _safe_record(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _slow_request_threshold_summary() -> dict[str, float]:
    return {
        "slow_api_threshold_ms": max(0.0, float(settings.api_slow_request_threshold_ms)),
        "slow_db_threshold_ms": max(0.0, float(settings.db_slow_query_threshold_ms)),
    }


def _mask_queue_payload_value(value: object, key: str = "") -> object:
    normalized_key = key.strip().lower()
    if normalized_key in {"authorization", "token", "password", "secret", "api_key", "apikey"}:
        return "[MASKED]"
    if normalized_key == "file_base64":
        return f"[OMITTED:{len(value)}]" if isinstance(value, str) else "[OMITTED]"
    if isinstance(value, dict):
        return {str(item_key): _mask_queue_payload_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_mask_queue_payload_value(item) for item in value]
    return value


def _queue_event_payload(event: ChannelQueueEvent) -> dict[str, object]:
    payload = _safe_record(getattr(event, "parameter_json", None))
    masked = _mask_queue_payload_value(payload)
    return masked if isinstance(masked, dict) else {}


def _search_blob(*values: object) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            parts.append(json.dumps(value, ensure_ascii=False, default=str))
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _log_payload(entry: dict[str, object]) -> dict[str, object]:
    return _safe_record(entry.get("payload"))


def _log_detail(payload: dict[str, object]) -> dict[str, object]:
    return _safe_record(payload.get("detail"))


def _ensure_system_common_variables(db: Session, organization: Organization) -> None:
    for legacy_name in LEGACY_SYSTEM_COMMON_VARIABLE_NAMES:
        legacy = db.scalar(
            select(CommonVariable).where(
                CommonVariable.organization_id == organization.id,
                CommonVariable.name == legacy_name,
                CommonVariable.deleted_at.is_(None),
            )
        )
        if legacy is not None:
            legacy.deleted_at = datetime.now(timezone.utc)
            db.add(legacy)

    for item in SYSTEM_COMMON_VARIABLES:
        db.execute(
            pg_insert(CommonVariable)
            .values(
                id=uuid4(),
                organization_id=organization.id,
                kind="system",
                name=item["name"],
                value=item["value"],
                description=item["description"],
                data_json={"protected": True},
                deleted_at=None,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "name"],
                set_={
                    "kind": "system",
                    "value": item["value"],
                    "description": item["description"],
                    "deleted_at": None,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        existing = db.scalar(
            select(CommonVariable).where(
                CommonVariable.organization_id == organization.id,
                CommonVariable.name == item["name"],
            )
        )
        if existing is not None:
            existing.data_json = _build_common_variable_json(existing, "SYSTEM")
            db.add(existing)


def _serialize_common_variable(variable: CommonVariable, updater_name: str) -> AdminCommonVariableItem:
    return AdminCommonVariableItem(
        id=variable.id,
        kind=variable.kind,  # type: ignore[arg-type]
        name=variable.name,
        value=variable.value,
        description=variable.description,
        updated_at=variable.updated_at,
        updater_name=updater_name,
        data_json=variable.data_json or {},
    )


def _default_message_category_label(category: str) -> str:
    return {
        "intent": "의도",
        "input": "입력",
        "error": "오류",
        "session": "세션",
        "runtime": "런타임",
    }.get(category, category)


def _default_message_definition(message_key: str, language: str = "ko") -> dict[str, str] | None:
    localized = DEFAULT_MESSAGE_CATALOGS.get(language, DEFAULT_MESSAGE_CATALOGS["ko"]).get(message_key)
    if localized is None:
        return DEFAULT_ADMIN_DEFAULT_MESSAGE_BY_KEY.get(message_key)
    category = DEFAULT_ADMIN_DEFAULT_MESSAGE_BY_KEY.get(message_key, {}).get("category", "runtime")
    return {"message_key": message_key, "category": category, **localized}


def _build_default_message_json(message: AdminDefaultMessage, updater_name: str | None = None) -> dict[str, object]:
    default_item = _default_message_definition(message.message_key, message.language)
    default_text = default_item["message_text"] if default_item is not None else None
    return {
        "id": str(message.id),
        "message_key": message.message_key,
        "message_name": message.message_name,
        "category": message.category,
        "category_label": _default_message_category_label(message.category),
        "language": message.language,
        "scope": message.scope,
        "scope_label": "전체" if message.scope == "global" else "그룹",
        "message_text": message.message_text,
        "default_message_text": default_text,
        "is_modified": default_text is not None and message.message_text != default_text,
        "description": message.description,
        "status": message.status,
        "status_label": "사용" if message.status == "active" else "미사용",
        "organization_id": str(message.organization_id),
        "updated_by": str(message.updated_by) if message.updated_by else None,
        "updater_name": updater_name,
        "protected": True,
    }


def _ensure_default_messages(db: Session, organization: Organization) -> None:
    for language, catalog in DEFAULT_MESSAGE_CATALOGS.items():
        for message_key, localized_definition in catalog.items():
            category = DEFAULT_ADMIN_DEFAULT_MESSAGE_BY_KEY.get(message_key, {}).get("category", "runtime")
            db.execute(
                pg_insert(AdminDefaultMessage)
                .values(
                    id=uuid4(),
                    organization_id=organization.id,
                    message_key=message_key,
                    message_name=localized_definition["message_name"],
                    category=category,
                    language=language,
                    scope="global",
                    message_text=localized_definition["message_text"],
                    description=localized_definition["description"],
                    status="active",
                    data_json={"protected": True},
                    deleted_at=None,
                )
                .on_conflict_do_update(
                    index_elements=["organization_id", "message_key", "language"],
                    set_={
                        "message_name": localized_definition["message_name"],
                        "category": category,
                        "description": localized_definition["description"],
                        "deleted_at": None,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            existing = db.scalar(
                select(AdminDefaultMessage).where(
                    AdminDefaultMessage.organization_id == organization.id,
                    AdminDefaultMessage.message_key == message_key,
                    AdminDefaultMessage.language == language,
                )
            )
            if existing is not None:
                existing.data_json = _build_default_message_json(existing, "SYSTEM")
                db.add(existing)


def _serialize_default_message(message: AdminDefaultMessage, updater_name: str) -> AdminDefaultMessageItem:
    default_item = _default_message_definition(message.message_key, message.language)
    default_text = default_item["message_text"] if default_item is not None else None
    return AdminDefaultMessageItem(
        id=message.id,
        message_key=message.message_key,
        message_name=message.message_name,
        category=message.category,
        category_label=_default_message_category_label(message.category),
        language=message.language,
        scope=message.scope,  # type: ignore[arg-type]
        scope_label="전체" if message.scope == "global" else "그룹",
        message_text=message.message_text,
        default_message_text=default_text,
        is_modified=default_text is not None and message.message_text != default_text,
        description=message.description,
        status=message.status,  # type: ignore[arg-type]
        status_label="사용" if message.status == "active" else "미사용",
        updated_at=message.updated_at,
        updater_name=updater_name,
        data_json=message.data_json or {},
    )

def _ensure_default_admin_channels(db: Session, organization: Organization) -> None:
    # 채널/템플릿의 첫 동시 조회는 해당 조직의 기존 transaction 안에서만 직렬화한다.
    db.execute(select(Organization.id).where(Organization.id == organization.id).with_for_update()).scalar_one()
    for item in DEFAULT_ADMIN_CHANNELS:
        existing = db.scalar(
            select(AdminChannel).where(
                AdminChannel.organization_id == organization.id,
                AdminChannel.code == item["code"],
                AdminChannel.deleted_at.is_(None),
            )
        )
        channel_config = _build_channel_config(
            str(item.get("provider") or "webchat"),
            str(item.get("renderer_type") or "webchat"),
            item.get("endpoint_url") if isinstance(item.get("endpoint_url"), str) else None,
            str(item.get("auth_type") or "none"),
            {},
        )
        if existing is not None:
            current_json = dict(existing.data_json or {})
            for key, value in channel_config.items():
                current_value = current_json.get(key)
                if current_value is None:
                    current_json[key] = value
                    continue
                if isinstance(current_value, str) and not current_value.strip():
                    current_json[key] = value
                    continue
                if key == "auth_config" and not isinstance(current_value, dict):
                    current_json[key] = value
            existing.data_json = current_json
            existing.data_json = _build_channel_json(existing, "SYSTEM", "SYSTEM")
            db.add(existing)
            continue
        channel = AdminChannel(
            organization_id=organization.id,
            code=item["code"],
            name=item["name"],
            description=item["description"],
            status="active",
            data_json=channel_config,
        )
        db.add(channel)
        db.flush()
        channel.data_json = _build_channel_json(channel, "SYSTEM", "SYSTEM")
        db.add(channel)


def _serialize_admin_channel(
    channel: AdminChannel,
    creator_name: str,
    updater_name: str,
) -> AdminChannelItem:
    return AdminChannelItem(
        id=channel.id,
        code=channel.code,
        name=channel.name,
        description=channel.description,
        status=channel.status,  # type: ignore[arg-type]
        status_label="사용" if channel.status == "active" else "미사용",
        creator_name=creator_name,
        updater_name=updater_name,
        updated_at=channel.updated_at,
        data_json=channel.data_json or {},
    )


def _channel_name_map(db: Session, organization_id: UUID) -> dict[str, str]:
    channels = db.execute(
        select(AdminChannel).where(
            AdminChannel.organization_id == organization_id,
            AdminChannel.deleted_at.is_(None),
        )
    ).scalars().all()
    return {channel.code: channel.name for channel in channels}


def _ensure_default_admin_templates(db: Session, organization: Organization) -> None:
    _ensure_default_admin_channels(db, organization)
    for item in DEFAULT_ADMIN_TEMPLATES:
        existing = db.scalar(
            select(AdminTemplate).where(
                AdminTemplate.organization_id == organization.id,
                AdminTemplate.channel_code == item["channel_code"],
                AdminTemplate.name == item["name"],
                AdminTemplate.deleted_at.is_(None),
            )
        )
        if existing is not None:
            continue
        template = AdminTemplate(
            organization_id=organization.id,
            channel_code=item["channel_code"],
            name=item["name"],
            renderer_type=item["renderer_type"],
            item_types=item["item_types"],
            description=item["description"],
            status="active",
            data_json={},
        )
        db.add(template)
        db.flush()
        template.data_json = _build_template_json(template, "Simulator", "SYSTEM", "SYSTEM")
        db.add(template)


def _serialize_admin_template(
    template: AdminTemplate,
    channel_name: str,
    creator_name: str,
    updater_name: str,
) -> AdminTemplateItem:
    item_types = [item.strip() for item in template.item_types.split(",") if item.strip()]
    return AdminTemplateItem(
        id=template.id,
        channel_code=template.channel_code,
        channel_name=channel_name,
        name=template.name,
        renderer_type=template.renderer_type,
        item_count=len(item_types),
        item_types=template.item_types,
        description=template.description,
        status=template.status,  # type: ignore[arg-type]
        status_label="사용" if template.status == "active" else "미사용",
        creator_name=creator_name,
        updater_name=updater_name,
        updated_at=template.updated_at,
        data_json=template.data_json or {},
    )


@router.get("/users")
def list_admin_users(
    request: Request,
    query: str | None = Query(default=None),
    group_id: UUID | None = Query(default=None),
    role_code: str | None = Query(default=None),
    account_status: str | None = Query(default=None),
    signup_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    users = db.execute(
        select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc())
    ).scalars().all()
    role_map = _get_user_role_map(db, [user.id for user in users])
    group_map = _get_group_name_map(db, [user.group_id for user in users if user.group_id is not None])

    signup_requests = db.execute(
        select(SignupRequest)
        .where(SignupRequest.status.in_(("pending", "rejected")))
        .order_by(SignupRequest.created_at.desc())
    ).scalars().all()
    signup_group_map = _get_group_name_map(db, [item.group_id for item in signup_requests])
    role_code_map = {
        role.code: role
        for role in db.execute(select(Role).where(Role.deleted_at.is_(None))).scalars().all()
    }

    items = [
        _serialize_user_list_item(
            user,
            group_map.get(user.group_id).name if user.group_id in group_map else "-",
            role_map.get(user.id),
        )
        for user in users
    ]
    items.extend(
        _serialize_signup_list_item(
            signup_request,
            signup_group_map.get(signup_request.group_id).name if signup_request.group_id in signup_group_map else "-",
            role_code_map.get(signup_request.requested_role_code),
        )
        for signup_request in signup_requests
    )

    items.sort(key=lambda item: item.requested_at, reverse=True)

    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in item.login_id.lower() or lowered in item.name.lower()
        ]
    if group_id is not None:
        filtered_items: list[AdminUserListItem] = []
        for item in items:
            if item.kind == "user" and any(user.id == item.id and user.group_id == group_id for user in users):
                filtered_items.append(item)
            if item.kind == "signup_request" and any(
                signup_request.id == item.id and signup_request.group_id == group_id
                for signup_request in signup_requests
            ):
                filtered_items.append(item)
        items = filtered_items
    if role_code:
        items = [item for item in items if item.role_code == role_code]
    if account_status:
        items = [item for item in items if item.account_status == _user_account_status_label(account_status)]
    if signup_status:
        items = [item for item in items if item.signup_status == _signup_status_label(signup_status)]

    return success_response(
        request,
        {
            "items": [item.model_dump() for item in items],
            "total": len(items),
        },
    )


@router.get("/users/{user_id}")
def get_admin_user_detail(
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    organization = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    group = db.scalar(select(Group).where(Group.id == user.group_id)) if user.group_id else None
    role = _get_user_role_map(db, [user.id]).get(user.id)

    detail = AdminUserDetail(
        id=user.id,
        kind="user",
        login_id=user.login_id,
        name=user.name,
        organization_name=organization.name if organization is not None else "-",
        group_name=group.name if group is not None else "-",
        group_id=group.id if group is not None else UUID(int=0),
        role_code=(role.code if role is not None else "curator"),  # type: ignore[arg-type]
        role_name=role.name if role is not None else "큐레이터",
        requested_at=user.created_at,
        signup_status="계정 승인",
        account_status=_user_account_status_label(user.status),
        preferred_language="ko",
        is_protected=user.is_protected,
        available_groups=_get_available_group_options(db, user.organization_id),
        data_json=user.data_json or {},
    )
    return success_response(request, detail.model_dump())


@router.patch("/users/{user_id}")
def update_admin_user_info(
    user_id: UUID,
    payload: UserInfoUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    current_role_codes = _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    _ensure_assignable_role(current_role_codes, payload.role_code)
    role = _get_role_by_code(db, payload.role_code)
    group = _get_group_or_404(db, payload.group_id)
    if group.organization_id != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="현재 서버에 속한 그룹만 선택할 수 있습니다.",
        )

    previous_role_codes = _get_user_role_codes(db, user.id)
    before_json = {
        "name": user.name,
        "group_id": str(user.group_id) if user.group_id else None,
        "status": user.status,
        "roles": previous_role_codes,
    }
    user.name = payload.name.strip()
    user.group_id = group.id
    user.status = payload.status

    existing_links = db.execute(select(UserRole).where(UserRole.user_id == user.id)).scalars().all()
    for link in existing_links:
        db.delete(link)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    organization = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    user.data_json = _build_user_json(user, [role.code], group, organization)
    db.add(user)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.user.update",
            target_type="user",
            target_id=user.id,
            before_json=before_json,
            after_json={
                "name": user.name,
                "group_id": str(group.id),
                "group_name": group.name,
                "status": user.status,
                "roles": [role.code],
            },
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "사용자 정보가 변경되었습니다."})


@router.patch("/users/{user_id}/status")
def update_admin_user_status(
    user_id: UUID,
    payload: UserStatusUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    before_status = user.status
    user.status = payload.status
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.user.status",
            target_type="user",
            target_id=user.id,
            before_json={"status": before_status},
            after_json={"status": payload.status},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.add(user)
    db.commit()

    return success_response(
        request,
        {"message": "계정 상태가 변경되었습니다."},
    )


@router.patch("/users/{user_id}/role")
def update_admin_user_role(
    user_id: UUID,
    payload: UserRoleUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    current_role_codes = _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    _ensure_assignable_role(current_role_codes, payload.role_code)
    new_role = _get_role_by_code(db, payload.role_code)
    existing_links = db.execute(select(UserRole).where(UserRole.user_id == user.id)).scalars().all()
    previous_role_codes = []
    for link in existing_links:
        role = db.scalar(select(Role).where(Role.id == link.role_id))
        if role is not None:
            previous_role_codes.append(role.code)
        db.delete(link)

    db.flush()
    db.add(UserRole(user_id=user.id, role_id=new_role.id))
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.user.role",
            target_type="user",
            target_id=user.id,
            before_json={"roles": previous_role_codes},
            after_json={"roles": [new_role.code]},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    return success_response(request, {"message": "사용자 역할이 변경되었습니다."})


@router.patch("/users/{user_id}/group")
def update_admin_user_group(
    user_id: UUID,
    payload: UserGroupUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")

    group = _get_group_or_404(db, payload.group_id)
    if group.organization_id != user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="현재 서버에 속한 그룹만 선택할 수 있습니다.",
        )

    previous_group = db.scalar(select(Group).where(Group.id == user.group_id)) if user.group_id else None
    user.group_id = group.id
    organization = db.scalar(select(Organization).where(Organization.id == user.organization_id))
    user.data_json = _build_user_json(user, _get_user_role_codes(db, user.id), group, organization)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.user.group",
            target_type="user",
            target_id=user.id,
            before_json={"group_id": str(previous_group.id) if previous_group else None},
            after_json={"group_id": str(group.id), "group_name": group.name},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.add(user)
    db.commit()

    return success_response(request, {"message": "사용자 그룹이 변경되었습니다."})


@router.delete("/users/{user_id}")
def delete_admin_user(
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    user = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다.")
    if user.is_protected or user.login_id == "master":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="기본 master 사용자는 삭제할 수 없습니다.",
        )

    user.deleted_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.user.delete",
            target_type="user",
            target_id=user.id,
            before_json={"login_id": user.login_id},
            after_json={"deleted": True},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.add(user)
    db.commit()

    return success_response(request, {"message": "사용자가 삭제되었습니다."})


@router.get("/signup-requests/{signup_request_id}")
def get_signup_request_detail(
    signup_request_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    current_role_codes = _require_admin_user(db, current_user)

    signup_request = db.scalar(select(SignupRequest).where(SignupRequest.id == signup_request_id))
    if signup_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="회원가입 신청을 찾을 수 없습니다.")

    organization = db.scalar(select(Organization).where(Organization.id == signup_request.organization_id))
    group = db.scalar(select(Group).where(Group.id == signup_request.group_id))
    role = _get_role_by_code(db, signup_request.requested_role_code)

    detail = AdminUserDetail(
        id=signup_request.id,
        kind="signup_request",
        login_id=signup_request.login_id,
        name=signup_request.name,
        organization_name=organization.name if organization is not None else "-",
        group_name=group.name if group is not None else "-",
        group_id=group.id if group is not None else UUID(int=0),
        role_code=signup_request.requested_role_code,  # type: ignore[arg-type]
        role_name=role.name,
        requested_at=signup_request.created_at,
        signup_status="승인 요청" if signup_request.status == "pending" else "승인 반려",
        account_status="활성",
        comment=signup_request.comment,
        preferred_language=signup_request.preferred_language,
        is_protected=False,
        available_groups=_get_available_group_options(db, signup_request.organization_id),
        data_json=signup_request.data_json or {},
    )
    return success_response(request, detail.model_dump())


@router.post("/signup-requests/{signup_request_id}/approve")
def approve_signup_request(
    signup_request_id: UUID,
    payload: SignupApprovalRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    current_role_codes = _require_admin_user(db, current_user)

    signup_request = db.scalar(
        select(SignupRequest).where(SignupRequest.id == signup_request_id, SignupRequest.status == "pending")
    )
    if signup_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="승인 가능한 회원가입 신청을 찾을 수 없습니다.",
        )

    existing_user = db.scalar(
        select(User).where(User.login_id == signup_request.login_id, User.deleted_at.is_(None))
    )
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 동일한 사용자 계정이 존재합니다.",
        )

    _ensure_assignable_role(current_role_codes, payload.role_code)
    role = _get_role_by_code(db, payload.role_code)
    target_group_id = payload.group_id or signup_request.group_id
    target_group = _get_group_or_404(db, target_group_id)
    if target_group.organization_id != signup_request.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="현재 서버에 속한 그룹만 선택할 수 있습니다.",
        )
    assert_license_allows_creation(db, signup_request.organization_id, "users")
    created_user = User(
        organization_id=signup_request.organization_id,
        group_id=target_group.id,
        login_id=signup_request.login_id,
        password_hash=signup_request.password_hash,
        name=signup_request.name,
        email=None,
        status="active",
        data_json={},
        is_protected=False,
    )
    db.add(created_user)
    db.flush()
    db.add(UserRole(user_id=created_user.id, role_id=role.id))
    organization = db.scalar(select(Organization).where(Organization.id == signup_request.organization_id))
    created_user.data_json = _build_user_json(created_user, [role.code], target_group, organization)

    signup_request.status = "approved"
    signup_request.requested_role_code = role.code
    signup_request.group_id = target_group.id
    signup_request.data_json = _build_signup_request_json(signup_request, target_group, organization)
    signup_request.reviewed_user_id = current_user.id
    signup_request.reviewed_at = datetime.now(timezone.utc)
    db.add(signup_request)

    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.signup.approve",
            target_type="signup_request",
            target_id=signup_request.id,
            after_json={
                "created_user_id": str(created_user.id),
                "role_code": role.code,
                "group_id": str(target_group.id),
                "group_name": target_group.name,
            },
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    return success_response(request, {"message": "회원가입 신청이 승인되었습니다."})


@router.post("/signup-requests/{signup_request_id}/reject")
def reject_signup_request(
    signup_request_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    signup_request = db.scalar(
        select(SignupRequest).where(SignupRequest.id == signup_request_id, SignupRequest.status == "pending")
    )
    if signup_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="반려 가능한 회원가입 신청을 찾을 수 없습니다.",
        )

    signup_request.status = "rejected"
    group = db.scalar(select(Group).where(Group.id == signup_request.group_id))
    organization = db.scalar(select(Organization).where(Organization.id == signup_request.organization_id))
    signup_request.data_json = _build_signup_request_json(signup_request, group, organization)
    signup_request.reviewed_user_id = current_user.id
    signup_request.reviewed_at = datetime.now(timezone.utc)
    db.add(signup_request)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.signup.reject",
            target_type="signup_request",
            target_id=signup_request.id,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    return success_response(request, {"message": "회원가입 신청이 반려되었습니다."})


@router.get("/login-history")
def list_login_history(
    request: Request,
    query: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    retention_days = max(settings.login_history_retention_days, 1)
    retention_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    logs = db.execute(
        select(AuditLog)
        .where(
            AuditLog.action_type.in_(tuple(LOGIN_HISTORY_ACTIONS)),
            AuditLog.created_at >= retention_cutoff,
        )
        .order_by(AuditLog.created_at.asc())
    ).scalars().all()

    user_ids = sorted(
        {
            log.actor_user_id
            for log in logs
            if log.actor_user_id is not None
        }
    )
    user_map = {
        user.id: user
        for user in db.execute(select(User).where(User.id.in_(user_ids), User.deleted_at.is_(None))).scalars().all()
    }
    role_map = _get_user_role_map(db, list(user_map.keys()))
    group_map = _get_group_name_map(db, [user.group_id for user in user_map.values() if user.group_id is not None])

    open_sessions: dict[UUID, list[AdminLoginHistoryItem]] = defaultdict(list)
    sessions: list[AdminLoginHistoryItem] = []

    for log in logs:
        user_id = log.actor_user_id
        if user_id is None or user_id not in user_map:
            continue
        user = user_map[user_id]
        role = role_map.get(user_id)
        group = group_map.get(user.group_id) if user.group_id is not None else None
        if log.action_type == "auth.login":
            if open_sessions[user_id]:
                previous_session = open_sessions[user_id].pop(0)
                previous_session.logout_at = log.created_at
                sessions.append(previous_session)
            open_sessions[user_id].append(
                AdminLoginHistoryItem(
                    id=str(log.id),
                    login_id=user.login_id,
                    name=user.name,
                    group_name=group.name if group is not None else "-",
                    role_name=role.name if role is not None else "-",
                    ip_address=log.ip_address,
                    login_at=log.created_at,
                    logout_at=None,
                )
            )
        elif open_sessions[user_id]:
            session = open_sessions[user_id].pop(0)
            session.logout_at = log.created_at
            sessions.append(session)

    for pending_sessions in open_sessions.values():
        sessions.extend(pending_sessions)

    sessions.sort(key=lambda item: item.login_at, reverse=True)

    if query:
        lowered = query.strip().lower()
        sessions = [
            item
            for item in sessions
            if lowered in item.login_id.lower() or lowered in item.name.lower()
        ]
    if from_date:
        from_datetime = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        sessions = [item for item in sessions if item.login_at >= from_datetime]
    if to_date:
        to_datetime = datetime.combine(to_date, time.max, tzinfo=timezone.utc)
        sessions = [item for item in sessions if item.login_at <= to_datetime]

    return success_response(
        request,
        {
            "items": [item.model_dump() for item in sessions],
            "total": len(sessions),
        },
        meta={"retention_days": retention_days},
    )


def _compact_training_history_json(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}

    compact: dict[str, object] = dict(value)
    diagnostics = compact.get("quality_diagnostics")
    if isinstance(diagnostics, dict):
        compact_diagnostics: dict[str, object] = {}
        settings = diagnostics.get("settings")
        summary = diagnostics.get("summary")
        items = diagnostics.get("items")
        if isinstance(settings, dict):
            compact_diagnostics["settings"] = settings
        if isinstance(summary, dict):
            compact_diagnostics["summary"] = summary
        if isinstance(items, list):
            compact_diagnostics["items"] = items[:30]
        compact["quality_diagnostics"] = compact_diagnostics
    return compact


def _version_training_state(version: BotVersion) -> dict[str, object]:
    version_json = _safe_record(version.version_json)
    system_config = _safe_record(version_json.get("system_config"))
    return _safe_record(system_config.get("nlu_training"))


def _version_training_history_json(version: BotVersion) -> dict[str, object]:
    version_json = _safe_record(version.version_json)
    system_config = _safe_record(version_json.get("system_config"))
    training = _safe_record(system_config.get("nlu_training"))
    evaluation = _safe_record(system_config.get("nlu_evaluation"))
    latest = _safe_record(evaluation.get("latest"))

    payload: dict[str, object] = dict(training)
    quality_diagnostics = _safe_record(evaluation.get("quality_diagnostics")) or _safe_record(latest.get("quality_diagnostics"))
    if quality_diagnostics:
        payload["quality_diagnostics"] = quality_diagnostics
    return _compact_training_history_json(payload)


def _version_bot_settings(version: BotVersion, bot: Bot) -> dict[str, object]:
    version_json = _safe_record(version.version_json)
    bot_data = _safe_record(bot.data_json)
    settings: dict[str, object] = {}
    settings["nlu_type"] = (
        version_json.get("nlu_type")
        or bot_data.get("nlu_type")
        or bot_data.get("nlu_engine")
        or "ml"
    )
    settings["nlu_model"] = (
        version_json.get("nlu_model")
        or bot_data.get("nlu_model")
        or bot_data.get("nlu_engine")
        or ("semantic_engine_default" if settings["nlu_type"] in {"semantic", "semantic_vector", "semantic_external"} else "deep_learning_lite")
    )
    return settings


def _first_text_value(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _training_engine_snapshot(source: object, version: BotVersion, bot: Bot) -> dict[str, object]:
    payload = _safe_record(source)
    fallback = _version_bot_settings(version, bot)
    nlu_type = _first_text_value(
        payload.get("nlu_type"),
        payload.get("engine_type"),
        payload.get("nluEngineType"),
        fallback.get("nlu_type"),
        "ml",
    )
    nlu_model = _first_text_value(
        payload.get("nlu_model"),
        payload.get("model"),
        payload.get("model_key"),
        payload.get("nluModel"),
        fallback.get("nlu_model"),
        "deep_learning_lite",
    )
    return {
        "nlu_type": nlu_type,
        "nlu_model": nlu_model,
    }


def _training_completed_at(started_at: datetime, completed_at: datetime) -> datetime:
    if completed_at.replace(microsecond=0) <= started_at.replace(microsecond=0):
        return started_at.replace(microsecond=0) + timedelta(seconds=1)
    return completed_at


def _count_rows(db: Session, statement: object) -> int:
    value = db.scalar(statement)
    return int(value or 0)


def _is_botstation_enabled(bot: Bot) -> bool:
    if bot.active_version_id is None:
        return False
    data = _safe_record(bot.data_json)
    settings_by_version = _safe_record(data.get("settings_by_version"))
    active_version_key = str(bot.active_version_id)
    candidates = [settings_by_version.get(active_version_key), data.get("botstation"), data.get("bot_station")]
    saw_botstation_config = False
    for candidate in candidates:
        candidate_record = _safe_record(candidate)
        station = _safe_record(
            candidate_record.get("botstation")
            or candidate_record.get("bot_station")
            or candidate_record.get("botStation")
            or candidate_record
        )
        if station:
            saw_botstation_config = True
        enabled = station.get("enabled") or station.get("connected") or station.get("is_enabled")
        status_value = str(station.get("status") or "").lower()
        if enabled is True or status_value in {"active", "enabled", "connected"}:
            return True
    if not saw_botstation_config:
        return True
    return False


def _queue_runtime_events(payload: object) -> list[dict[str, object]]:
    result = _safe_record(payload)
    events = result.get("runtimeEvents")
    if not isinstance(events, list):
        events = result.get("runtime_events")
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _runtime_problem_event(events: list[dict[str, object]]) -> dict[str, object] | None:
    for event in reversed(events):
        if str(event.get("level") or "").lower() in {"warning", "error"}:
            return event
    return None


def _runtime_summary(events: list[dict[str, object]], fallback: str) -> str:
    problem = _runtime_problem_event(events)
    if problem:
        return str(problem.get("message") or problem.get("event") or "런타임 오류")
    if events:
        last_event = events[-1]
        return str(last_event.get("message") or last_event.get("event") or fallback)
    return fallback


def _runtime_event_location(event: dict[str, object] | None) -> str:
    if not event:
        return "-"
    dialog_name = str(event.get("dialogName") or event.get("dialog_name") or "").strip()
    node_title = str(event.get("nodeTitle") or event.get("node_title") or "").strip()
    node_kind = str(event.get("nodeKind") or event.get("node_kind") or "").strip()
    node_id = str(event.get("nodeId") or event.get("node_id") or "").strip()
    location = " / ".join(value for value in (dialog_name, node_title) if value)
    node_meta = " · ".join(value for value in (node_kind, node_id) if value)
    if location and node_meta:
        return f"{location} ({node_meta})"
    return location or node_meta or "-"


def _runtime_intent_name(*sources: object, fallback: str = "-") -> str:
    def first_from_record(record: dict[str, object]) -> str:
        nested_records = [
            record,
            _safe_record(record.get("data")),
            _safe_record(record.get("metadata")),
            _safe_record(record.get("result")),
            _safe_record(record.get("detail")),
            _safe_record(record.get("log")),
            _safe_record(record.get("startDialog") or record.get("start_dialog")),
            _safe_record(record.get("startModule") or record.get("start_module")),
            _safe_record(record.get("startIntent") or record.get("start_intent")),
            _safe_record(record.get("selectedDialog") or record.get("selected_dialog")),
            _safe_record(record.get("selectedModule") or record.get("selected_module")),
            _safe_record(record.get("selectedIntent") or record.get("selected_intent")),
            _safe_record(record.get("dialog") or record.get("intent")),
            _safe_record(record.get("module")),
        ]
        keys = (
            "sessionStartModuleName",
            "session_start_module_name",
            "sessionStartIntentName",
            "session_start_intent_name",
            "startDialogName",
            "start_dialog_name",
            "startDialogId",
            "start_dialog_id",
            "startModuleName",
            "start_module_name",
            "startIntentName",
            "start_intent_name",
            "intentName",
            "intent_name",
            "moduleName",
            "module_name",
            "selectedIntentName",
            "selected_intent_name",
            "selectedModuleName",
            "selected_module_name",
            "selectedDialogName",
            "selected_dialog_name",
            "dialogName",
            "dialog_name",
            "cardName",
            "card_name",
            "nodeTitle",
            "node_title",
            "displayName",
            "display_name",
            "name",
        )
        for nested in nested_records:
            for key in keys:
                value = str(nested.get(key) or "").strip()
                if value and value != "-":
                    return value
        return ""

    for source in sources:
        value = first_from_record(_safe_record(source))
        if value:
            return value
    return fallback


def _is_weak_intent_or_module_name(value: object) -> bool:
    text = str(value or "").strip()
    normalized = re.sub(r"\s+", " ", text).lower()
    if not normalized:
        return True
    if normalized in {"-", "시나리오 실행", "대화 시작", "start", "startup"}:
        return True
    return bool(re.match(r"^(talk|end|rich form|condition|jump|function|api|answer|message)(?:\s*\d+)?$", normalized))


def _prefer_start_intent_or_module_name(current: object, candidate: object) -> str:
    current_text = str(current or "").strip()
    candidate_text = str(candidate or "").strip()
    if not candidate_text or candidate_text == "-":
      return current_text or "-"
    if not current_text or current_text == "-":
      return candidate_text
    if _is_weak_intent_or_module_name(current_text) and not _is_weak_intent_or_module_name(candidate_text):
      return candidate_text
    return current_text


def _recent_runtime_event_rows(
    queue_events: list[ChannelQueueEvent],
    bots: dict[UUID, Bot],
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for queue_event in queue_events:
        bot = bots.get(queue_event.bot_id)
        queue_payload = _queue_event_payload(queue_event)
        queue_result = _safe_record(queue_event.result_json)
        for index, runtime_event in enumerate(_queue_runtime_events(queue_event.result_json)):
            level = str(runtime_event.get("level") or "info")
            rows.append(
                {
                    "id": f"Runtime:{queue_event.id}:{runtime_event.get('time') or index}",
                    "source": queue_event.channel_type,
                    "level": level,
                    "event": str(runtime_event.get("event") or "channel.runtime.event"),
                    "message": str(runtime_event.get("message") or "-"),
                    "bot_name": bot.name if bot else "-",
                    "dialog_name": _runtime_intent_name(runtime_event, fallback=queue_event.intent_name or "-"),
                    "node_title": str(runtime_event.get("nodeTitle") or "-"),
                    "occurred_at": str(runtime_event.get("time") or queue_event.status_changed_at.isoformat()),
                    "data_json": {
                        "queue_event_id": str(queue_event.id),
                        "channel_type": queue_event.channel_type,
                        "room_id": str(queue_event.room_id) if queue_event.room_id else None,
                        "bot_id": str(queue_event.bot_id),
                        "queue_status": queue_event.status,
                        "queue_error_message": queue_event.error_message,
                        "completion_reason": str(queue_result.get("completionReason") or ""),
                        "dialog_ended": queue_result.get("dialogEnded") is True,
                        "session_ended": queue_result.get("sessionEnded") is True,
                        "runtime_event_index": index,
                        "problem_location": _runtime_event_location(runtime_event),
                        "runtime_event": runtime_event,
                        "queue_payload": queue_payload,
                        "queue_result": queue_result,
                    },
                }
            )
    rows.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    return rows[:limit]


def _queue_status_counts(queue_events: list[ChannelQueueEvent]) -> dict[str, int]:
    return {
        "queue_queued": len([event for event in queue_events if event.status == "queued"]),
        "queue_processing": len([event for event in queue_events if event.status == "processing"]),
        "queue_completed": len([event for event in queue_events if event.status == "completed"]),
        "queue_failed": len([event for event in queue_events if event.status == "failed" or event.error_message]),
    }


def _runtime_channel_type(channel_code: str | None) -> str | None:
    normalized = str(channel_code or "").strip().upper()
    if not normalized:
        return None
    return {
        "WEBCHAT": "webchat",
        "KAKAO": "kakao",
        "TEAMS": "ms-teams",
        "MS-TEAMS": "ms-teams",
    }.get(normalized, normalized.lower())


def _channel_display_name(channel_type: str | None) -> str:
    normalized = str(channel_type or "").strip().lower()
    if not normalized:
        return "-"
    return {
        "simulator": "시뮬레이터",
        "webchat": "webchat",
        "kakao": "Kakao",
        "ms-teams": "MS Teams",
        "teams": "MS Teams",
        "training": "NLU 학습",
    }.get(normalized, channel_type or "-")


def _recent_runtime_errors(
    db: Session,
    cutoff: datetime,
    organization_id: UUID,
    *,
    bot_ids: set[UUID] | None = None,
    channel_code: str | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    bot_id_strings = {str(bot_id) for bot_id in bot_ids} if bot_ids is not None else None
    runtime_channel_type = _runtime_channel_type(channel_code)
    for source, log_dir, prefix in (
        ("Simulator", "simulator", "simulator"),
        ("API", "api-function", "api-function"),
    ):
        for entry in _read_web_jsonl_logs(log_dir, prefix, max_files=10):
            server_time = _parse_datetime(entry.get("serverTime"))
            if server_time is None or server_time < cutoff:
                continue
            payload = _log_payload(entry)
            if bot_id_strings is not None and str(payload.get("botId") or "") not in bot_id_strings:
                continue
            if channel_code and source == "Simulator" and channel_code not in {"SIMULATOR", "SM_CHAT"}:
                continue
            if channel_code and source == "API" and str(payload.get("channelCode") or "SM_CHAT").upper() != channel_code:
                continue
            detail = _log_detail(payload)
            runtime = _safe_record(detail.get("runtime"))
            event = str(payload.get("event") or "")
            status_value = detail.get("status") or payload.get("status")
            try:
                status_code = int(status_value) if status_value is not None else 0
            except (TypeError, ValueError):
                status_code = 0
            if "failed" not in event and "exception" not in event and status_code < 400:
                continue
            rows.append(
                {
                    "id": f"{source}:{detail.get('analysisId') or entry.get('serverTime') or len(rows)}",
                    "source": source,
                    "event": event or "error",
                    "message": str(detail.get("message") or payload.get("message") or detail.get("statusText") or "오류가 기록되었습니다."),
                    "bot_name": str(payload.get("botName") or detail.get("botName") or "-"),
                    "occurred_at": server_time.isoformat(),
                    "data_json": {
                        "log_source": source,
                        "server_time": entry.get("serverTime"),
                        "event": event or "error",
                        "problem_location": _runtime_event_location(runtime),
                        "payload": payload,
                        "detail": detail,
                    },
                }
            )
    queue_filters = [
        Bot.organization_id == organization_id,
        ChannelQueueEvent.created_at >= cutoff,
        ChannelQueueEvent.deleted_at.is_(None),
    ]
    if bot_ids is not None:
        if bot_ids:
            queue_filters.append(ChannelQueueEvent.bot_id.in_(bot_ids))
        else:
            queue_filters.append(ChannelQueueEvent.bot_id.is_(None))
    if runtime_channel_type:
        queue_filters.append(ChannelQueueEvent.channel_type == runtime_channel_type)
    queue_events = db.scalars(
        select(ChannelQueueEvent)
        .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        .where(*queue_filters)
        .order_by(ChannelQueueEvent.created_at.desc())
        .limit(100)
    ).all()
    bot_ids = {event.bot_id for event in queue_events}
    bots = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(bot_ids))).all()} if bot_ids else {}
    for event in queue_events:
        bot = bots.get(event.bot_id)
        runtime_events = _queue_runtime_events(event.result_json)
        problem_event = _runtime_problem_event(runtime_events)
        if event.status == "failed" or event.error_message:
            rows.append(
                {
                    "id": f"Queue:{event.id}",
                    "source": _channel_display_name(event.channel_type),
                    "event": "channel.queue.failed",
                    "message": event.error_message or "채널 Queue 처리에 실패했습니다.",
                    "bot_name": bot.name if bot else "-",
                    "occurred_at": event.status_changed_at.isoformat(),
                    "data_json": {
                        "queue_event_id": str(event.id),
                        "channel_type": event.channel_type,
                        "room_id": str(event.room_id) if event.room_id else None,
                        "bot_id": str(event.bot_id),
                        "status": event.status,
                        "error_message": event.error_message,
                        "problem_location": _runtime_event_location(problem_event),
                        "latest_problem_event": problem_event or {},
                        "payload_json": _queue_event_payload(event),
                        "result_json": event.result_json or {},
                    },
                }
            )
        for runtime_event in runtime_events:
            if str(runtime_event.get("level") or "").lower() not in {"warning", "error"}:
                continue
            rows.append(
                {
                    "id": f"Runtime:{event.id}:{runtime_event.get('time') or len(rows)}",
                    "source": _channel_display_name(event.channel_type),
                    "event": str(runtime_event.get("event") or "channel.runtime.event"),
                    "message": str(runtime_event.get("message") or "채널 런타임 오류가 기록되었습니다."),
                    "bot_name": bot.name if bot else "-",
                    "occurred_at": str(runtime_event.get("time") or event.status_changed_at.isoformat()),
                    "data_json": {
                        "queue_event_id": str(event.id),
                        "channel_type": event.channel_type,
                        "room_id": str(event.room_id) if event.room_id else None,
                        "bot_id": str(event.bot_id),
                        "problem_location": _runtime_event_location(runtime_event),
                        "runtime_event": runtime_event,
                        "payload_json": _queue_event_payload(event),
                        "result_json": event.result_json or {},
                    },
                }
            )
    rows.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    return rows[:10]


@router.get("/operations-dashboard")
def get_operations_dashboard(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    hours: int | None = Query(default=None, ge=1, le=72),
    group_id: UUID | None = Query(default=None),
    bot_id: UUID | None = Query(default=None),
    channel_code: str | None = Query(default=None),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    organization = _get_default_organization(db)
    now = datetime.now(timezone.utc)
    period_hours = hours if hours is not None else days * 24
    cutoff = now - timedelta(hours=period_hours)
    period_label = f"{period_hours}시간" if hours is not None else f"{days}일"
    normalized_channel_code = channel_code.strip().upper() if channel_code else None
    runtime_channel_type = _runtime_channel_type(normalized_channel_code)
    dashboard_cache_key = _operations_dashboard_cache_key(
        organization.id,
        days=days,
        hours=hours,
        group_id=group_id,
        bot_id=bot_id,
        channel_code=normalized_channel_code,
    )
    if refresh:
        _purge_operations_dashboard_cache()
    else:
        cached_dashboard = _get_operations_dashboard_cache(dashboard_cache_key)
        if cached_dashboard is not None:
            return success_response(request, cached_dashboard)

    bot_query = select(Bot).where(Bot.organization_id == organization.id, Bot.deleted_at.is_(None))
    if group_id is not None:
        bot_query = bot_query.where(Bot.group_id == group_id)
    if bot_id is not None:
        bot_query = bot_query.where(Bot.id == bot_id)

    bots = db.execute(bot_query).scalars().all()
    filtered_bot_ids: set[UUID] | None = None
    if group_id is not None or bot_id is not None:
        filtered_bot_ids = {bot.id for bot in bots}

    active_bots = [bot for bot in bots if bot.status == "active"]
    operating_bots = [bot for bot in active_bots if bot.active_version_id is not None]
    botstation_bots = [bot for bot in operating_bots if _is_botstation_enabled(bot)]

    version_query = select(func.count()).select_from(BotVersion).where(BotVersion.deleted_at.is_(None))
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            version_query = version_query.where(BotVersion.bot_id.in_(filtered_bot_ids))
        else:
            version_query = version_query.where(BotVersion.bot_id.is_(None))
    total_versions = _count_rows(db, version_query)

    channel_query = select(func.count()).select_from(AdminChannel).where(
        AdminChannel.organization_id == organization.id,
        AdminChannel.status == "active",
        AdminChannel.deleted_at.is_(None),
    )
    if normalized_channel_code:
        channel_query = channel_query.where(AdminChannel.code == normalized_channel_code)
    active_channels = _count_rows(db, channel_query)

    room_filters = [ChannelRoom.created_at >= cutoff, ChannelRoom.deleted_at.is_(None)]
    if runtime_channel_type:
        room_filters.append(ChannelRoom.channel_type == runtime_channel_type)
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            room_filters.append(ChannelRoom.bot_id.in_(filtered_bot_ids))
        else:
            room_filters.append(ChannelRoom.bot_id.is_(None))

    channel_rooms = _count_rows(
        db,
        select(func.count()).select_from(ChannelRoom).where(*room_filters),
    )

    message_filters = [
        ChannelMessage.created_at >= cutoff,
        ChannelMessage.deleted_at.is_(None),
        ChannelRoom.deleted_at.is_(None),
    ]
    if runtime_channel_type:
        message_filters.append(ChannelMessage.channel_type == runtime_channel_type)
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            message_filters.append(ChannelRoom.bot_id.in_(filtered_bot_ids))
        else:
            message_filters.append(ChannelRoom.bot_id.is_(None))

    user_messages = _count_rows(
        db,
        select(func.count())
        .select_from(ChannelMessage)
        .join(ChannelRoom, ChannelMessage.room_id == ChannelRoom.id)
        .where(*message_filters, ChannelMessage.participant_kind == "user"),
    )
    bot_messages = _count_rows(
        db,
        select(func.count())
        .select_from(ChannelMessage)
        .join(ChannelRoom, ChannelMessage.room_id == ChannelRoom.id)
        .where(*message_filters, ChannelMessage.participant_kind == "bot"),
    )

    selected_bot_id_strings = {str(value) for value in filtered_bot_ids} if filtered_bot_ids is not None else None
    simulator_entries = _read_web_jsonl_logs("simulator", "simulator", max_files=20)
    api_calls = 0
    api_errors = 0
    intent_fallbacks = 0
    for entry in simulator_entries:
        server_time = _parse_datetime(entry.get("serverTime"))
        if server_time is None or server_time < cutoff:
            continue
        payload = _log_payload(entry)
        if selected_bot_id_strings is not None and str(payload.get("botId") or "") not in selected_bot_id_strings:
            continue
        if normalized_channel_code and normalized_channel_code not in {"SIMULATOR", "SM_CHAT"}:
            continue
        event = str(payload.get("event") or "")
        if event in {"simulator.function_call_success", "simulator.function_call_failed", "simulator.function_call_exception"}:
            api_calls += 1
            if event != "simulator.function_call_success":
                api_errors += 1
        detail = _log_detail(payload)
        result_type = str(detail.get("resultType") or "")
        selected_intent = str(detail.get("selectedIntentName") or "")
        if result_type == "fallback" or selected_intent in {"", "의도 미분류"}:
            intent_fallbacks += 1

    training_query = (
        select(AuditLog.after_json)
        .where(AuditLog.action_type == "bot.version.nlu.train", AuditLog.created_at >= cutoff)
        .order_by(AuditLog.created_at.desc())
    )
    if filtered_bot_ids is not None:
        version_ids = set(
            db.scalars(
                select(BotVersion.id).where(
                    BotVersion.deleted_at.is_(None),
                    BotVersion.bot_id.in_(filtered_bot_ids) if filtered_bot_ids else BotVersion.bot_id.is_(None),
                )
            ).all()
        )
        if version_ids:
            training_query = training_query.where(AuditLog.target_id.in_(version_ids))
        else:
            training_query = training_query.where(AuditLog.target_id.is_(None))
    training_logs = db.execute(training_query).scalars().all()
    training_success = 0
    training_failed = 0
    for after_json_value in training_logs:
        after_json = _safe_record(after_json_value)
        counts = _safe_record(after_json.get("counts"))
        document_count = int(counts.get("intent_documents") or 0) + int(counts.get("entity_documents") or 0)
        if document_count > 0:
            training_success += 1
        else:
            training_failed += 1

    queue_filters = [
        Bot.organization_id == organization.id,
        ChannelQueueEvent.created_at >= cutoff,
        ChannelQueueEvent.deleted_at.is_(None),
    ]
    if runtime_channel_type:
        queue_filters.append(ChannelQueueEvent.channel_type == runtime_channel_type)
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            queue_filters.append(ChannelQueueEvent.bot_id.in_(filtered_bot_ids))
        else:
            queue_filters.append(ChannelQueueEvent.bot_id.is_(None))
    queue_events = db.scalars(
        select(ChannelQueueEvent)
        .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        .where(*queue_filters)
        .order_by(ChannelQueueEvent.created_at.desc())
        .limit(500)
    ).all()
    queue_counts = _queue_status_counts(queue_events)
    queue_bot_ids = {event.bot_id for event in queue_events}
    queue_bots = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(queue_bot_ids))).all()} if queue_bot_ids else {}
    runtime_event_count = 0
    runtime_problem_count = 0
    for event in queue_events:
        for runtime_event in _queue_runtime_events(event.result_json):
            runtime_event_count += 1
            if str(runtime_event.get("level") or "").lower() in {"warning", "error"}:
                runtime_problem_count += 1
            event_name = str(runtime_event.get("event") or "")
            if event_name.startswith("channel.runtime.function_"):
                api_calls += 1
                if str(runtime_event.get("level") or "").lower() in {"warning", "error"}:
                    api_errors += 1

    recent_errors = _recent_runtime_errors(
        db,
        cutoff,
        organization.id,
        bot_ids=filtered_bot_ids,
        channel_code=normalized_channel_code,
    )
    recent_runtime_events = _recent_runtime_event_rows(queue_events, queue_bots)
    system_error_count, recent_system_errors = _recent_system_error_snapshot(cutoff)
    recent_system_log_items, _ = _recent_system_log_items(cutoff)
    slow_api_requests = _recent_system_event_count(
        cutoff,
        "api.slow_request",
        recent_items=recent_system_log_items,
    )
    slow_db_requests = _recent_slow_db_request_count(
        cutoff,
        settings.db_slow_query_threshold_ms,
        recent_items=recent_system_log_items,
    )
    recent_slow_requests = _recent_slow_request_rows(
        cutoff,
        settings.db_slow_query_threshold_ms,
        limit=50,
        recent_items=recent_system_log_items,
    )
    recent_slow_request_summary = _slow_request_summary_rows(recent_slow_requests)
    conflict_query = select(func.count()).select_from(AuditLog).where(
        AuditLog.action_type == "edit_lock.conflict",
        AuditLog.created_at >= cutoff,
    )
    if group_id is not None or filtered_bot_ids is not None:
        conflict_lock_filters = [EditLock.organization_id == organization.id]
        if group_id is not None:
            conflict_lock_filters.append(EditLock.group_id == group_id)
        if filtered_bot_ids is not None:
            if filtered_bot_ids:
                conflict_lock_filters.append(EditLock.bot_id.in_(filtered_bot_ids))
            else:
                conflict_lock_filters.append(EditLock.bot_id.is_(None))
        conflict_lock_ids = select(EditLock.id).where(*conflict_lock_filters)
        conflict_query = conflict_query.where(AuditLog.target_id.in_(conflict_lock_ids))
    edit_lock_conflicts = _count_rows(db, conflict_query)

    lock_filters = [
        EditLock.organization_id == organization.id,
        EditLock.released_at.is_(None),
        EditLock.expires_at > now,
    ]
    if group_id is not None:
        lock_filters.append(EditLock.group_id == group_id)
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            lock_filters.append(EditLock.bot_id.in_(filtered_bot_ids))
        else:
            lock_filters.append(EditLock.bot_id.is_(None))
    active_lock_count = _count_rows(db, select(func.count()).select_from(EditLock).where(*lock_filters))
    expired_lock_filters = [
        EditLock.organization_id == organization.id,
        EditLock.released_at.is_(None),
        EditLock.expires_at <= now,
    ]
    if group_id is not None:
        expired_lock_filters.append(EditLock.group_id == group_id)
    if filtered_bot_ids is not None:
        if filtered_bot_ids:
            expired_lock_filters.append(EditLock.bot_id.in_(filtered_bot_ids))
        else:
            expired_lock_filters.append(EditLock.bot_id.is_(None))
    expired_lock_count = _count_rows(db, select(func.count()).select_from(EditLock).where(*expired_lock_filters))
    active_locks = db.scalars(
        select(EditLock)
        .where(*lock_filters)
        .order_by(EditLock.last_seen_at.desc())
        .limit(20)
    ).all()
    lock_bot_ids = {lock.bot_id for lock in active_locks}
    lock_version_ids = {lock.version_id for lock in active_locks}
    lock_bots = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(lock_bot_ids))).all()} if lock_bot_ids else {}
    lock_versions = {version.id: version for version in db.scalars(select(BotVersion).where(BotVersion.id.in_(lock_version_ids))).all()} if lock_version_ids else {}
    active_lock_rows = [
        {
            "id": str(lock.id),
            "bot_id": str(lock.bot_id),
            "bot_name": lock_bots.get(lock.bot_id).name if lock.bot_id in lock_bots else "-",
            "version_id": str(lock.version_id),
            "version_name": lock_versions.get(lock.version_id).name if lock.version_id in lock_versions else "-",
            "dialog_id": lock.dialog_id,
            "area": lock.area,
            "owner_login_id": lock.owner_login_id,
            "owner_name": lock.owner_name,
            "expires_at": lock.expires_at.isoformat(),
            "last_seen_at": lock.last_seen_at.isoformat(),
        }
        for lock in active_locks
    ]
    summary = {
        "period_days": days,
        "period_hours": period_hours,
        "period_label": period_label,
        "total_bots": len(bots),
        "active_bots": len(active_bots),
        "total_versions": total_versions,
        "operating_bots": len(operating_bots),
        "botstation_connected_bots": len(botstation_bots),
        "active_channels": active_channels,
        "channel_rooms": channel_rooms,
        "user_messages": user_messages,
        "bot_messages": bot_messages,
        "api_calls": api_calls,
        "api_errors": api_errors,
        "intent_fallbacks": intent_fallbacks,
        "training_success": training_success,
        "training_failed": training_failed,
        "queue_events": len(queue_events),
        **queue_counts,
        "runtime_events": runtime_event_count,
        "runtime_problem_events": runtime_problem_count,
        "system_errors": system_error_count,
        "slow_api_requests": slow_api_requests,
        "slow_db_requests": slow_db_requests,
        **_slow_request_threshold_summary(),
        "recent_error_count": len(recent_errors),
        "active_edit_locks": active_lock_count,
        "expired_edit_locks": expired_lock_count,
        "edit_lock_conflicts": edit_lock_conflicts,
    }
    cache_status = cache_status_snapshot()
    nlu_acceleration = {
        "ml": get_ml_acceleration_status(),
        "semantic": _vector_worker_acceleration_status(),
    }
    dashboard_payload: dict[str, object] = {
        "summary": summary,
        "alerts": _operations_alerts(summary, cache_status),
        "recent_errors": recent_errors,
        "recent_runtime_events": recent_runtime_events,
        "recent_system_errors": recent_system_errors,
        "recent_slow_requests": recent_slow_requests,
        "recent_slow_request_summary": recent_slow_request_summary,
        "active_edit_locks": active_lock_rows,
        "cache": cache_status,
        "nlu_acceleration": nlu_acceleration,
        "generated_at": now.isoformat(),
    }
    _set_operations_dashboard_cache(dashboard_cache_key, dashboard_payload)
    return success_response(request, dashboard_payload)


@router.get("/operations-dashboard/version-integrity")
def get_operations_dashboard_version_integrity(
    request: Request,
    group_id: UUID | None = Query(default=None),
    bot_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)
    organization = _get_default_organization(db)
    integrity_status = _cached_version_integrity_status(
        db,
        organization.id,
        group_id=group_id,
        bot_id=bot_id,
    )
    dialog_split = integrity_status["dialog_split"]
    read_snapshot = integrity_status["read_snapshot"]
    summary = {
        "version_storage_total_versions": dialog_split["total_versions"],
        "version_storage_split_versions": dialog_split["split_versions"],
        "version_storage_missing_versions": dialog_split["missing_versions"],
        "version_storage_mismatch_versions": dialog_split["mismatch_versions"],
        "version_storage_expected_dialog_rows": dialog_split["expected_dialog_rows"],
        "version_storage_actual_dialog_rows": dialog_split["actual_dialog_rows"],
        "version_storage_expected_graph_rows": dialog_split["expected_graph_rows"],
        "version_storage_actual_graph_rows": dialog_split["actual_graph_rows"],
        "version_read_snapshot_total_versions": read_snapshot["total_versions"],
        "version_read_snapshot_complete_versions": read_snapshot["complete_versions"],
        "version_read_snapshot_missing_versions": read_snapshot["missing_versions"],
        "version_read_snapshot_missing_asset_counts": read_snapshot["missing_asset_counts"],
        "version_read_snapshot_missing_scenario_validation": read_snapshot["missing_scenario_validation"],
        "version_read_snapshot_missing_nlu_training": read_snapshot["missing_nlu_training"],
        "version_read_snapshot_missing_entities": read_snapshot["missing_entities"],
        "version_read_snapshot_missing_dictionary": read_snapshot["missing_dictionary"],
        "version_read_snapshot_missing_apis": read_snapshot["missing_apis"],
        "version_read_snapshot_missing_system_config": read_snapshot["missing_system_config"],
    }
    alerts = [
        item
        for item in _operations_alerts(summary, {})
        if str(item.get("code") or "").startswith("version_")
    ]
    return success_response(
        request,
        {
            "summary": summary,
            "alerts": alerts,
            "version_storage_issues": dialog_split.get("issues", []),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.post("/cache/purge")
def purge_admin_cache(
    payload: AdminCachePurgeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    patterns = {
        "version_sections": ["version:*"],
        "studio_read_models": ["version:*", "bot-*"],
    }
    domain_patterns = patterns[payload.domain]
    results = [purge_cache_pattern(pattern) for pattern in domain_patterns]
    purged = sum(int(result.get("purged") or 0) for result in results)
    statuses = {str(result.get("status")) for result in results}
    failed_reasons = [str(result.get("reason")) for result in results if result.get("status") == "failed" and result.get("reason")]
    skipped_reasons = [str(result.get("reason")) for result in results if result.get("status") == "skipped" and result.get("reason")]
    if "failed" in statuses:
        status_value = "failed"
        reason = " / ".join(failed_reasons) or "cache purge failed"
    elif statuses == {"skipped"}:
        status_value = "skipped"
        reason = " / ".join(skipped_reasons) or "cache_unavailable"
    else:
        status_value = "purged"
        reason = None
    return success_response(
        request,
        {
            "domain": payload.domain,
            "status": status_value,
            "purged": purged,
            "pattern": ",".join(domain_patterns),
            "reason": reason,
            "cache": cache_status_snapshot(),
        },
    )


def _version_storage_filtered_versions(
    db: Session,
    organization_id: UUID,
    *,
    group_id: UUID | None = None,
    bot_id: UUID | None = None,
    limit: int | None = None,
) -> list[tuple[BotVersion, Bot]]:
    query = (
        select(BotVersion, Bot)
        .join(Bot, Bot.id == BotVersion.bot_id)
        .where(
            Bot.organization_id == organization_id,
            Bot.deleted_at.is_(None),
            BotVersion.deleted_at.is_(None),
        )
        .order_by(BotVersion.updated_at.desc(), BotVersion.created_at.desc())
    )
    if group_id is not None:
        query = query.where(Bot.group_id == group_id)
    if bot_id is not None:
        query = query.where(Bot.id == bot_id)
    if limit is not None:
        query = query.limit(limit)
    return list(db.execute(query).all())


def _version_integrity_cache_key(
    db: Session,
    organization_id: UUID,
    *,
    group_id: UUID | None = None,
    bot_id: UUID | None = None,
) -> str:
    query = (
        select(Bot.id, Bot.updated_at, BotVersion.id, BotVersion.updated_at)
        .join(BotVersion, BotVersion.bot_id == Bot.id)
        .where(
            Bot.organization_id == organization_id,
            Bot.deleted_at.is_(None),
            BotVersion.deleted_at.is_(None),
        )
        .order_by(Bot.id, BotVersion.id)
    )
    if group_id is not None:
        query = query.where(Bot.group_id == group_id)
    if bot_id is not None:
        query = query.where(Bot.id == bot_id)
    stamps = [
        ":".join(
            [
                str(bot_id_value),
                bot_updated_at.isoformat() if bot_updated_at else "unknown",
                str(version_id),
                version_updated_at.isoformat() if version_updated_at else "unknown",
            ]
        )
        for bot_id_value, bot_updated_at, version_id, version_updated_at in db.execute(query).all()
    ]
    digest = hashlib.sha1("|".join(stamps).encode("utf-8")).hexdigest()
    return f"admin:version-integrity:v1:{organization_id}:{group_id or 'all'}:{bot_id or 'all'}:{digest}"


def _cached_version_integrity_status(
    db: Session,
    organization_id: UUID,
    *,
    group_id: UUID | None = None,
    bot_id: UUID | None = None,
) -> dict[str, dict[str, object]]:
    cache_key = _version_integrity_cache_key(
        db,
        organization_id,
        group_id=group_id,
        bot_id=bot_id,
    )

    def _produce() -> dict[str, dict[str, object]]:
        return {
            "dialog_split": _version_dialog_split_status(
                db,
                organization_id,
                group_id=group_id,
                bot_id=bot_id,
                include_issues=True,
            ),
            "read_snapshot": _version_read_snapshot_status(
                db,
                organization_id,
                group_id=group_id,
                bot_id=bot_id,
            ),
        }

    return cache_aside_json(
        cache_key,
        _produce,
        ttl_seconds=settings.cache_default_ttl_seconds,
    )


def _purge_version_integrity_cache(organization_id: UUID) -> int:
    return int(
        purge_cache_pattern(f"admin:version-integrity:v1:{organization_id}:*").get("purged") or 0
    )

def _version_read_snapshot_status(
    db: Session,
    organization_id: UUID,
    *,
    group_id: UUID | None = None,
    bot_id: UUID | None = None,
) -> dict[str, int]:
    filters = [
        Bot.organization_id == organization_id,
        Bot.deleted_at.is_(None),
        BotVersion.deleted_at.is_(None),
    ]
    if group_id is not None:
        filters.append(Bot.group_id == group_id)
    if bot_id is not None:
        filters.append(Bot.id == bot_id)

    def _count_with(*extra_filters: object) -> int:
        return _count_rows(
            db,
            select(func.count())
            .select_from(BotVersion)
            .join(Bot, Bot.id == BotVersion.bot_id)
            .where(*filters, *extra_filters),
        )

    total_versions = _count_with()
    missing_asset_counts = _count_with(BotVersion.asset_counts_json.is_(None))
    missing_scenario_validation = _count_with(BotVersion.scenario_validation_json.is_(None))
    missing_nlu_training = _count_with(BotVersion.nlu_training_json.is_(None))
    missing_entities = _count_with(BotVersion.entities_json.is_(None))
    missing_dictionary = _count_with(BotVersion.dictionary_json.is_(None))
    missing_apis = _count_with(BotVersion.apis_json.is_(None))
    missing_system_config = _count_with(BotVersion.system_config_json.is_(None))
    missing_versions = _count_with(
        or_(
            BotVersion.asset_counts_json.is_(None),
            BotVersion.scenario_validation_json.is_(None),
            BotVersion.nlu_training_json.is_(None),
            BotVersion.entities_json.is_(None),
            BotVersion.dictionary_json.is_(None),
            BotVersion.apis_json.is_(None),
            BotVersion.system_config_json.is_(None),
        )
    )
    return {
        "total_versions": total_versions,
        "complete_versions": max(total_versions - missing_versions, 0),
        "missing_versions": missing_versions,
        "missing_asset_counts": missing_asset_counts,
        "missing_scenario_validation": missing_scenario_validation,
        "missing_nlu_training": missing_nlu_training,
        "missing_entities": missing_entities,
        "missing_dictionary": missing_dictionary,
        "missing_apis": missing_apis,
        "missing_system_config": missing_system_config,
    }


def _assign_version_read_snapshot(version: BotVersion, version_json: dict[str, object]) -> None:
    normalized = normalize_version_document(version_json)
    system_config = normalized.get("system_config")
    nlu_training = system_config.get("nlu_training") if isinstance(system_config, dict) else {}
    version.asset_counts_json = build_version_asset_counts(normalized)
    version.scenario_validation_json = scenario_validation_from_version(normalized)
    version.nlu_training_json = nlu_training if isinstance(nlu_training, dict) else {}
    version.entities_json = [item for item in normalized["entities"] if isinstance(item, dict)]
    version.dictionary_json = [item for item in normalized["dictionary"] if isinstance(item, dict)]
    version.apis_json = [item for item in normalized["apis"] if isinstance(item, dict)]
    version.system_config_json = system_config if isinstance(system_config, dict) else {}


def _split_rows_by_version(rows: list[VersionDialogAsset | VersionDialogFlowGraph]) -> dict[UUID, list[object]]:
    grouped: dict[UUID, list[object]] = defaultdict(list)
    for row in rows:
        grouped[row.version_id].append(row)
    return grouped


def _version_dialog_split_summary(
    versions: list[BotVersion],
    dialog_rows_by_version: dict[UUID, list[object]],
    graph_rows_by_version: dict[UUID, list[object]],
) -> dict[str, int]:
    expected_dialog_rows = 0
    expected_graph_rows = 0
    actual_dialog_rows = 0
    actual_graph_rows = 0
    split_versions = 0
    missing_versions = 0
    mismatch_versions = 0
    dialog_mismatch_versions = 0
    graph_mismatch_versions = 0

    for version in versions:
        expected_dialog_ids = {
            str(row["dialog_id"])
            for row in build_version_dialog_asset_rows(version.version_json)
        }
        expected_graph_ids = {
            str(row["dialog_id"])
            for row in build_version_dialog_flow_graph_rows(version.version_json)
        }
        actual_dialog_ids = {
            str(getattr(row, "dialog_id", ""))
            for row in dialog_rows_by_version.get(version.id, [])
        }
        actual_graph_ids = {
            str(getattr(row, "dialog_id", ""))
            for row in graph_rows_by_version.get(version.id, [])
        }

        expected_dialog_rows += len(expected_dialog_ids)
        expected_graph_rows += len(expected_graph_ids)
        actual_dialog_rows += len(actual_dialog_ids)
        actual_graph_rows += len(actual_graph_ids)

        has_expected = bool(expected_dialog_ids or expected_graph_ids)
        has_actual = bool(actual_dialog_ids or actual_graph_ids)
        if has_actual:
            split_versions += 1
        if has_expected and not has_actual:
            missing_versions += 1

        dialog_mismatch = expected_dialog_ids != actual_dialog_ids
        graph_mismatch = expected_graph_ids != actual_graph_ids
        if dialog_mismatch:
            dialog_mismatch_versions += 1
        if graph_mismatch:
            graph_mismatch_versions += 1
        if dialog_mismatch or graph_mismatch:
            mismatch_versions += 1

    return {
        "total_versions": len(versions),
        "split_versions": split_versions,
        "missing_versions": missing_versions,
        "mismatch_versions": mismatch_versions,
        "dialog_mismatch_versions": dialog_mismatch_versions,
        "graph_mismatch_versions": graph_mismatch_versions,
        "expected_dialog_rows": expected_dialog_rows,
        "expected_graph_rows": expected_graph_rows,
        "actual_dialog_rows": actual_dialog_rows,
        "actual_graph_rows": actual_graph_rows,
    }


def _version_dialog_split_issue_rows(
    version_pairs: list[tuple[BotVersion, Bot]],
    dialog_rows_by_version: dict[UUID, list[object]],
    graph_rows_by_version: dict[UUID, list[object]],
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for version, bot in version_pairs:
        expected_dialog_ids = {
            str(row["dialog_id"])
            for row in build_version_dialog_asset_rows(version.version_json)
        }
        expected_graph_ids = {
            str(row["dialog_id"])
            for row in build_version_dialog_flow_graph_rows(version.version_json)
        }
        actual_dialog_ids = {
            str(getattr(row, "dialog_id", ""))
            for row in dialog_rows_by_version.get(version.id, [])
        }
        actual_graph_ids = {
            str(getattr(row, "dialog_id", ""))
            for row in graph_rows_by_version.get(version.id, [])
        }

        missing_dialog_ids = sorted(expected_dialog_ids - actual_dialog_ids)
        extra_dialog_ids = sorted(actual_dialog_ids - expected_dialog_ids)
        missing_graph_ids = sorted(expected_graph_ids - actual_graph_ids)
        extra_graph_ids = sorted(actual_graph_ids - expected_graph_ids)
        if not (missing_dialog_ids or extra_dialog_ids or missing_graph_ids or extra_graph_ids):
            continue

        has_expected = bool(expected_dialog_ids or expected_graph_ids)
        has_actual = bool(actual_dialog_ids or actual_graph_ids)
        rows.append(
            {
                "bot_id": str(bot.id),
                "bot_name": bot.name,
                "version_id": str(version.id),
                "version_name": version.name,
                "version_no": version.version_no,
                "status": "missing" if has_expected and not has_actual else "mismatch",
                "expected_dialog_rows": len(expected_dialog_ids),
                "actual_dialog_rows": len(actual_dialog_ids),
                "expected_graph_rows": len(expected_graph_ids),
                "actual_graph_rows": len(actual_graph_ids),
                "missing_dialog_ids": missing_dialog_ids[:10],
                "extra_dialog_ids": extra_dialog_ids[:10],
                "missing_graph_ids": missing_graph_ids[:10],
                "extra_graph_ids": extra_graph_ids[:10],
                "updated_at": version.updated_at.isoformat() if version.updated_at else None,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _version_dialog_split_status(
    db: Session,
    organization_id: UUID,
    *,
    group_id: UUID | None = None,
    bot_id: UUID | None = None,
    include_issues: bool = False,
) -> dict[str, object]:
    version_pairs = _version_storage_filtered_versions(
        db,
        organization_id,
        group_id=group_id,
        bot_id=bot_id,
    )
    versions = [version for version, _bot in version_pairs]
    version_ids = [version.id for version in versions]
    if not version_ids:
        result: dict[str, object] = _version_dialog_split_summary([], {}, {})
        if include_issues:
            result["issues"] = []
        return result

    try:
        dialog_rows = db.scalars(
            select(VersionDialogAsset).where(
                VersionDialogAsset.version_id.in_(version_ids),
                VersionDialogAsset.deleted_at.is_(None),
            )
        ).all()
        graph_rows = db.scalars(
            select(VersionDialogFlowGraph).where(
                VersionDialogFlowGraph.version_id.in_(version_ids),
                VersionDialogFlowGraph.deleted_at.is_(None),
            )
        ).all()
    except (OperationalError, ProgrammingError):
        db.rollback()
        result = _version_dialog_split_summary(versions, {}, {})
        if include_issues:
            result["issues"] = _version_dialog_split_issue_rows(version_pairs, {}, {})
        return result
    dialog_rows_by_version = _split_rows_by_version(dialog_rows)
    graph_rows_by_version = _split_rows_by_version(graph_rows)
    result = _version_dialog_split_summary(
        versions,
        dialog_rows_by_version,
        graph_rows_by_version,
    )
    if include_issues:
        result["issues"] = _version_dialog_split_issue_rows(
            version_pairs,
            dialog_rows_by_version,
            graph_rows_by_version,
        )
    return result


@router.get("/version-storage/status")
def get_version_storage_status(
    request: Request,
    group_id: UUID | None = Query(default=None),
    bot_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    organization = _get_default_organization(db)
    return success_response(
        request,
        {
            "dialog_split": _version_dialog_split_status(
                db,
                organization.id,
                group_id=group_id,
                bot_id=bot_id,
                include_issues=True,
            )
        },
    )


@router.post("/version-storage/backfill")
def backfill_version_storage(
    payload: AdminVersionStorageBackfillRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    organization = _get_default_organization(db)
    version_pairs = _version_storage_filtered_versions(
        db,
        organization.id,
        group_id=payload.group_id,
        bot_id=payload.bot_id,
        limit=payload.limit,
    )

    processed_versions = 0
    failed_versions: list[dict[str, object]] = []
    expected_dialog_rows = 0
    expected_graph_rows = 0
    for version, bot in version_pairs:
        dialog_rows = build_version_dialog_asset_rows(version.version_json)
        graph_rows = build_version_dialog_flow_graph_rows(version.version_json)
        expected_dialog_rows += len(dialog_rows)
        expected_graph_rows += len(graph_rows)
        if payload.dry_run:
            processed_versions += 1
            continue
        try:
            sync_version_dialog_split_tables(
                db,
                bot,
                version,
                version.version_json,
                include_dialogs=True,
                include_graphs=True,
            )
            db.flush()
            db.commit()
            processed_versions += 1
        except Exception as exc:  # pragma: no cover - defensive rollback path
            db.rollback()
            failed_versions.append(
                {
                    "version_id": str(version.id),
                    "bot_id": str(bot.id),
                    "message": str(exc),
                }
            )

    if not payload.dry_run:
        _purge_version_integrity_cache(organization.id)

    status_snapshot = _version_dialog_split_status(
        db,
        organization.id,
        group_id=payload.group_id,
        bot_id=payload.bot_id,
    )
    return success_response(
        request,
        {
            "dry_run": payload.dry_run,
            "requested_limit": payload.limit,
            "selected_versions": len(version_pairs),
            "processed_versions": processed_versions,
            "failed_versions": failed_versions,
            "expected_dialog_rows": expected_dialog_rows,
            "expected_graph_rows": expected_graph_rows,
            "dialog_split": status_snapshot,
        },
    )


@router.post("/version-read-snapshots/backfill")
def backfill_version_read_snapshots(
    payload: AdminVersionStorageBackfillRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    organization = _get_default_organization(db)
    version_pairs = _version_storage_filtered_versions(
        db,
        organization.id,
        group_id=payload.group_id,
        bot_id=payload.bot_id,
        limit=payload.limit,
    )

    processed_versions = 0
    failed_versions: list[dict[str, object]] = []
    purged_cache_keys = 0
    for version, bot in version_pairs:
        if payload.dry_run:
            processed_versions += 1
            continue
        try:
            _assign_version_read_snapshot(version, version.version_json)
            db.flush()
            db.commit()
            purged_cache_keys += int(purge_cache_pattern(f"version:{version.id}:*").get("purged") or 0)
            processed_versions += 1
        except Exception as exc:  # pragma: no cover - defensive rollback path
            db.rollback()
            failed_versions.append(
                {
                    "version_id": str(version.id),
                    "bot_id": str(bot.id),
                    "message": str(exc),
                }
            )

    if not payload.dry_run:
        _purge_version_integrity_cache(organization.id)

    status_snapshot = _version_read_snapshot_status(
        db,
        organization.id,
        group_id=payload.group_id,
        bot_id=payload.bot_id,
    )
    return success_response(
        request,
        {
            "dry_run": payload.dry_run,
            "requested_limit": payload.limit,
            "selected_versions": len(version_pairs),
            "processed_versions": processed_versions,
            "failed_versions": failed_versions,
            "purged_cache_keys": purged_cache_keys,
            "read_snapshot": status_snapshot,
        },
    )


def _parse_admin_filter_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        if len(stripped) == 10:
            parsed = datetime.fromisoformat(stripped).replace(tzinfo=timezone.utc)
            if end_of_day:
                return parsed + timedelta(days=1) - timedelta(microseconds=1)
            return parsed
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _audit_log_summary(log: AuditLog) -> str:
    before_json = _safe_record(log.before_json)
    after_json = _safe_record(log.after_json)
    message = after_json.get("message") or before_json.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    result = after_json.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    changed_keys = sorted(set(before_json.keys()) | set(after_json.keys()))
    if changed_keys:
        preview = ", ".join(changed_keys[:5])
        if len(changed_keys) > 5:
            preview = f"{preview} 외 {len(changed_keys) - 5}개"
        return f"{preview} 변경"
    return log.action_type

SYSTEM_LOG_FILES = {
    "app": "app.log",
    "error": "error.log",
}


def _system_log_item(log_file: str, line_no: int, line: str) -> dict[str, object]:
    stripped = line.strip()
    payload: dict[str, object]
    try:
        loaded = json.loads(stripped)
        payload = loaded if isinstance(loaded, dict) else {"message": stripped}
    except json.JSONDecodeError:
        payload = {"message": stripped}

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    status_code = payload.get("status_code")
    if status_code is not None:
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = None

    return {
        "id": f"{log_file}:{line_no}",
        "time": payload.get("time"),
        "level": payload.get("level") or "",
        "logger": payload.get("logger") or "",
        "event": payload.get("event"),
        "message": payload.get("message") or "",
        "method": payload.get("method"),
        "path": payload.get("path"),
        "status_code": status_code,
        "elapsed_ms": payload.get("elapsed_ms"),
        "client": payload.get("client"),
        "request_id": payload.get("request_id"),
        "data": data,
        "exception": payload.get("exception"),
        "raw": stripped,
    }


def _recent_system_error_snapshot(cutoff: datetime, *, limit: int = 20) -> tuple[int, list[dict[str, object]]]:
    log_file = SYSTEM_LOG_FILES["error"]
    rows: list[dict[str, object]] = []
    total = 0
    recent_items, _ = _recent_system_log_items(cutoff, log_file="error")
    for item in recent_items:
        occurred_at = _parse_datetime(item.get("time"))
        if occurred_at is None:
            continue
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        if occurred_at < cutoff:
            break
        level = str(item.get("level") or "").lower()
        if level and level not in {"error", "critical", "exception"}:
            continue
        total += 1
        if len(rows) >= limit:
            continue
        rows.append(
            {
                "id": item["id"],
                "source": "System",
                "level": item.get("level") or "error",
                "event": item.get("event") or "system.error",
                "message": item.get("message") or "시스템 오류가 기록되었습니다.",
                "logger": item.get("logger") or "-",
                "path": item.get("path"),
                "status_code": item.get("status_code"),
                "request_id": item.get("request_id"),
                "occurred_at": occurred_at.isoformat(),
                "data_json": {
                    "log_file": log_file,
                    "log_position": str(item.get("id") or "").rsplit(":", 1)[-1],
                    "data": item.get("data") or {},
                    "exception": item.get("exception"),
                    "raw": item.get("raw"),
                },
            }
        )
    return total, rows


def _operations_alerts(summary: dict[str, int], cache_status: dict[str, object]) -> list[dict[str, object]]:
    alerts: list[dict[str, object]] = []
    period_label = str(summary.get("period_label") or f"{summary.get('period_days')}일")

    def add_alert(level: str, code: str, title: str, message: str, value: int | float | str) -> None:
        alerts.append(
            {
                "level": level,
                "code": code,
                "title": title,
                "message": message,
                "value": value,
            }
        )

    system_errors = int(summary.get("system_errors") or 0)
    if system_errors > 0:
        add_alert(
            "critical",
            "system_errors",
            "시스템 오류 발생",
            f"최근 {period_label} 동안 시스템 오류가 {system_errors}건 기록되었습니다.",
            system_errors,
        )

    queue_failed = int(summary.get("queue_failed") or 0)
    if queue_failed > 0:
        add_alert(
            "critical",
            "queue_failed",
            "Queue 실패 발생",
            f"최근 {period_label} 동안 Queue 실패가 {queue_failed}건 기록되었습니다.",
            queue_failed,
        )

    slow_api_requests = int(summary.get("slow_api_requests") or 0)
    if slow_api_requests > 0:
        add_alert(
            "warning",
            "slow_api_requests",
            "느린 API 발생",
            f"설정된 느린 API 기준을 초과한 요청이 {slow_api_requests}건 있습니다.",
            slow_api_requests,
        )

    slow_db_requests = int(summary.get("slow_db_requests") or 0)
    if slow_db_requests > 0:
        add_alert(
            "warning",
            "slow_db_requests",
            "DB 조회 지연 발생",
            f"DB 조회 누적 시간이 기준을 초과한 요청이 {slow_db_requests}건 있습니다.",
            slow_db_requests,
        )

    storage_mismatch = int(summary.get("version_storage_mismatch_versions") or 0)
    if storage_mismatch > 0:
        add_alert(
            "warning",
            "version_storage_mismatch",
            "DB 분리 데이터 불일치",
            f"version_json과 분리 테이블의 대화/그래프 키가 다른 버전이 {storage_mismatch}건 있습니다.",
            storage_mismatch,
        )

    storage_missing = int(summary.get("version_storage_missing_versions") or 0)
    if storage_missing > 0:
        add_alert(
            "info",
            "version_storage_missing",
            "DB 분리 Backfill 필요",
            f"분리 테이블에 아직 반영되지 않은 버전이 {storage_missing}건 있습니다.",
            storage_missing,
        )

    read_snapshot_missing = int(summary.get("version_read_snapshot_missing_versions") or 0)
    if read_snapshot_missing > 0:
        add_alert(
            "info",
            "version_read_snapshot_missing",
            "버전 요약 스냅샷 Backfill 필요",
            f"버전 요약 read snapshot이 아직 채워지지 않은 버전이 {read_snapshot_missing}건 있습니다.",
            read_snapshot_missing,
        )

    cache_enabled = bool(cache_status.get("enabled"))
    cache_available = bool(cache_status.get("available"))
    if cache_enabled and not cache_available:
        add_alert(
            "warning",
            "cache_unavailable",
            "Redis 캐시 Fallback",
            "캐시가 켜져 있지만 Redis를 사용할 수 없어 DB 조회로 대체 중입니다.",
            "fallback",
        )

    cache_error_count = (
        int(cache_status.get("read_errors") or 0)
        + int(cache_status.get("write_errors") or 0)
        + int(cache_status.get("purge_errors") or 0)
    )
    if cache_error_count > 0:
        add_alert(
            "warning",
            "cache_errors",
            "캐시 오류 발생",
            f"캐시 read/write/purge 오류가 {cache_error_count}건 기록되었습니다.",
            cache_error_count,
        )

    cache_reads = int(cache_status.get("hits") or 0) + int(cache_status.get("misses") or 0)
    cache_hit_rate = float(cache_status.get("hit_rate") or 0)
    if cache_enabled and cache_reads >= 10 and cache_hit_rate < 70:
        add_alert(
            "warning",
            "cache_hit_rate_low",
            "캐시 적중률 낮음",
            f"캐시 적중률이 {cache_hit_rate}%입니다. 기준값 70% 미만입니다.",
            cache_hit_rate,
        )

    cache_memory_usage = cache_status.get("memory_usage_percent")
    if isinstance(cache_memory_usage, int | float) and cache_memory_usage >= 80:
        add_alert(
            "warning",
            "redis_memory_high",
            "Redis 메모리 사용량 높음",
            f"Redis 메모리 사용량이 {cache_memory_usage}%입니다. 기준값 80% 이상입니다.",
            cache_memory_usage,
        )

    runtime_problem_events = int(summary.get("runtime_problem_events") or 0)
    api_errors = int(summary.get("api_errors") or 0)
    if runtime_problem_events > 0 or api_errors > 0:
        add_alert(
            "warning",
            "runtime_or_api_errors",
            "실행/API 확인 필요",
            f"실행 경고/오류 {runtime_problem_events}건, API 오류 {api_errors}건이 있습니다.",
            runtime_problem_events + api_errors,
        )

    training_failed = int(summary.get("training_failed") or 0)
    if training_failed > 0:
        add_alert(
            "warning",
            "training_failed",
            "학습 결과 확인 필요",
            f"학습 확인이 필요한 이력이 {training_failed}건 있습니다.",
            training_failed,
        )

    active_edit_locks = int(summary.get("active_edit_locks") or 0)
    if active_edit_locks > 0:
        add_alert(
            "info",
            "active_edit_locks",
            "편집 잠금 활성",
            f"현재 편집 잠금 {active_edit_locks}건이 유지 중입니다.",
            active_edit_locks,
        )

    edit_lock_conflicts = int(summary.get("edit_lock_conflicts") or 0)
    if edit_lock_conflicts > 0:
        add_alert(
            "warning",
            "edit_lock_conflicts",
            "편집 충돌 발생",
            f"조회 기간 동안 다른 사용자의 편집 잠금으로 진입이 제한된 사례가 {edit_lock_conflicts}건 있습니다.",
            edit_lock_conflicts,
        )

    expired_edit_locks = int(summary.get("expired_edit_locks") or 0)
    if expired_edit_locks > 0:
        add_alert(
            "warning",
            "expired_edit_locks",
            "만료된 편집 잠금 남음",
            f"해제되지 않은 채 만료된 편집 잠금이 {expired_edit_locks}건 있습니다.",
            expired_edit_locks,
        )

    return alerts


def _recent_system_log_items(
    cutoff: datetime,
    *,
    log_file: str = "app",
) -> tuple[list[dict[str, object]], dict[str, object]]:
    resolved_log_file = SYSTEM_LOG_FILES.get(log_file, SYSTEM_LOG_FILES["app"])
    log_path = ROOT_DIR / "logs" / "api" / resolved_log_file
    log_lines, truncated = _read_bounded_log_tail(
        log_path,
        max_bytes=settings.admin_log_scan_max_bytes,
        max_lines=settings.admin_log_scan_max_lines,
    )
    items: list[dict[str, object]] = []
    for log_position, line in reversed(log_lines):
        if not line.strip():
            continue
        item = _system_log_item(resolved_log_file, log_position, line)
        occurred_at = _parse_datetime(item.get("time"))
        if occurred_at is None:
            continue
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        if occurred_at < cutoff:
            break
        items.append(item)
    return items, {
        "truncated": truncated,
        "scanned_lines": len(log_lines),
        "scan_limit_bytes": max(1024, int(settings.admin_log_scan_max_bytes)),
        "scan_limit_lines": max(1, int(settings.admin_log_scan_max_lines)),
        "from_time": cutoff.isoformat(),
    }


def _recent_system_event_count(
    cutoff: datetime,
    event_name: str,
    *,
    log_file: str = "app",
    recent_items: list[dict[str, object]] | None = None,
) -> int:
    items = recent_items if recent_items is not None else _recent_system_log_items(cutoff, log_file=log_file)[0]
    target_event = event_name.strip().lower()
    return sum(1 for item in items if str(item.get("event") or "").lower() == target_event)


def _recent_slow_db_request_count(
    cutoff: datetime,
    threshold_ms: float,
    *,
    log_file: str = "app",
    recent_items: list[dict[str, object]] | None = None,
) -> int:
    if threshold_ms <= 0:
        return 0
    items = recent_items if recent_items is not None else _recent_system_log_items(cutoff, log_file=log_file)[0]
    total = 0
    for item in items:
        if item.get("event") != "api.request":
            continue
        data = _safe_record(item.get("data"))
        try:
            db_duration_ms = float(data.get("db_duration_ms") or 0)
        except (TypeError, ValueError):
            db_duration_ms = 0
        if db_duration_ms >= threshold_ms:
            total += 1
    return total


def _recent_slow_request_rows(
    cutoff: datetime,
    db_threshold_ms: float,
    *,
    limit: int = 20,
    log_file: str = "app",
    recent_items: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    items = recent_items if recent_items is not None else _recent_system_log_items(cutoff, log_file=log_file)[0]
    rows: list[dict[str, object]] = []
    for item in items:
        if len(rows) >= limit:
            break
        occurred_at = _parse_datetime(item.get("time"))
        if occurred_at is None:
            continue
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        event = str(item.get("event") or "")
        data = _safe_record(item.get("data"))
        try:
            db_duration_ms = float(data.get("db_duration_ms") or 0)
        except (TypeError, ValueError):
            db_duration_ms = 0.0
        is_api_slow = event == "api.slow_request"
        is_db_slow = event == "api.request" and db_threshold_ms > 0 and db_duration_ms >= db_threshold_ms
        if not is_api_slow and not is_db_slow:
            continue
        try:
            db_query_count = int(data.get("db_query_count") or 0)
        except (TypeError, ValueError):
            db_query_count = 0
        try:
            slow_threshold_ms = float(data.get("slow_threshold_ms") or settings.api_slow_request_threshold_ms)
        except (TypeError, ValueError):
            slow_threshold_ms = float(settings.api_slow_request_threshold_ms)

        rows.append(
            {
                "id": str(item.get("id") or f"{log_file}:unknown"),
                "kind": "api" if is_api_slow else "db",
                "event": event,
                "method": item.get("method") or data.get("method") or "-",
                "path": item.get("path") or data.get("path") or "-",
                "status_code": item.get("status_code"),
                "elapsed_ms": item.get("elapsed_ms"),
                "db_duration_ms": db_duration_ms,
                "db_query_count": db_query_count,
                "threshold_ms": slow_threshold_ms if is_api_slow else db_threshold_ms,
                "request_id": item.get("request_id"),
                "occurred_at": occurred_at.isoformat(),
            }
        )
    return rows


def _slow_request_summary_rows(rows: list[dict[str, object]], *, limit: int = 10) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        kind = str(row.get("kind") or "-")
        method = str(row.get("method") or "-")
        path = str(row.get("path") or "-")
        key = (kind, method, path)
        elapsed_ms = _safe_float(row.get("elapsed_ms"))
        db_duration_ms = _safe_float(row.get("db_duration_ms"))
        occurred_at = str(row.get("occurred_at") or "")
        current = grouped.setdefault(
            key,
            {
                "id": f"{kind}:{method}:{path}",
                "kind": kind,
                "method": method,
                "path": path,
                "count": 0,
                "elapsed_total_ms": 0.0,
                "db_total_ms": 0.0,
                "max_elapsed_ms": 0.0,
                "max_db_duration_ms": 0.0,
                "latest_occurred_at": occurred_at,
            },
        )
        current["count"] = int(current["count"]) + 1
        current["elapsed_total_ms"] = float(current["elapsed_total_ms"]) + elapsed_ms
        current["db_total_ms"] = float(current["db_total_ms"]) + db_duration_ms
        current["max_elapsed_ms"] = max(float(current["max_elapsed_ms"]), elapsed_ms)
        current["max_db_duration_ms"] = max(float(current["max_db_duration_ms"]), db_duration_ms)
        if occurred_at > str(current.get("latest_occurred_at") or ""):
            current["latest_occurred_at"] = occurred_at

    summary_rows: list[dict[str, object]] = []
    for item in grouped.values():
        count = max(1, int(item["count"]))
        summary_rows.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "method": item["method"],
                "path": item["path"],
                "count": count,
                "max_elapsed_ms": round(float(item["max_elapsed_ms"]), 2),
                "avg_elapsed_ms": round(float(item["elapsed_total_ms"]) / count, 2),
                "max_db_duration_ms": round(float(item["max_db_duration_ms"]), 2),
                "avg_db_duration_ms": round(float(item["db_total_ms"]) / count, 2),
                "latest_occurred_at": item["latest_occurred_at"],
            }
        )
    summary_rows.sort(
        key=lambda item: (
            int(item.get("count") or 0),
            float(item.get("max_elapsed_ms") or 0),
            str(item.get("latest_occurred_at") or ""),
        ),
        reverse=True,
    )
    return summary_rows[:limit]


@router.get("/system-logs")
def list_system_logs(
    request: Request,
    file: str = Query(default="app"),
    query: str | None = Query(default=None),
    level: str | None = Query(default=None),
    event: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=24 * 30),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    log_file = SYSTEM_LOG_FILES.get(file, SYSTEM_LOG_FILES["app"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, scan_metadata = _recent_system_log_items(cutoff, log_file=file)
    scanned_total = len(items)

    if level:
        target_level = level.strip().lower()
        items = [item for item in items if str(item.get("level") or "").lower() == target_level]
    if event:
        target_event = event.strip().lower()
        items = [item for item in items if target_event in str(item.get("event") or "").lower()]
    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in str(item.get("time") or "").lower()
            or lowered in str(item.get("level") or "").lower()
            or lowered in str(item.get("logger") or "").lower()
            or lowered in str(item.get("event") or "").lower()
            or lowered in str(item.get("message") or "").lower()
            or lowered in str(item.get("method") or "").lower()
            or lowered in str(item.get("path") or "").lower()
            or lowered in str(item.get("status_code") or "").lower()
            or lowered in str(item.get("request_id") or "").lower()
            or lowered in str(item.get("raw") or "").lower()
        ]

    limited_items = items[:limit]
    return success_response(
        request,
        {
            "items": limited_items,
            "total": len(items),
            "log_file": log_file,
            "scanned_total": scanned_total,
            **scan_metadata,
        },
    )

@router.get("/audit-logs")
def list_audit_logs(
    request: Request,
    query: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    conditions = []
    if action_type:
        conditions.append(AuditLog.action_type == action_type)
    if target_type:
        conditions.append(AuditLog.target_type == target_type)
    parsed_from = _parse_admin_filter_datetime(from_date)
    parsed_to = _parse_admin_filter_datetime(to_date, end_of_day=True)
    if parsed_from is not None:
        conditions.append(AuditLog.created_at >= parsed_from)
    if parsed_to is not None:
        conditions.append(AuditLog.created_at <= parsed_to)

    statement = select(AuditLog)
    if conditions:
        statement = statement.where(*conditions)
    logs = db.execute(statement.order_by(AuditLog.created_at.desc())).scalars().all()
    actor_names = _actor_name_map(db, {log.actor_user_id for log in logs if log.actor_user_id is not None})

    items: list[dict[str, object]] = []
    for log in logs:
        actor = actor_names.get(log.actor_user_id, "SYSTEM") if log.actor_user_id is not None else "SYSTEM"
        before_json = _safe_record(log.before_json)
        after_json = _safe_record(log.after_json)
        item = {
            "id": str(log.id),
            "action_type": log.action_type,
            "target_type": log.target_type,
            "target_id": str(log.target_id) if log.target_id is not None else None,
            "actor_login_id": actor,
            "ip_address": log.ip_address,
            "created_at": log.created_at.isoformat(),
            "summary": _audit_log_summary(log),
            "data_json": {
                "before": before_json,
                "after": after_json,
            },
        }
        items.append(item)

    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in str(item.get("action_type") or "").lower()
            or lowered in str(item.get("target_type") or "").lower()
            or lowered in str(item.get("target_id") or "").lower()
            or lowered in str(item.get("actor_login_id") or "").lower()
            or lowered in str(item.get("ip_address") or "").lower()
            or lowered in str(item.get("summary") or "").lower()
        ]

    return success_response(request, {"items": items, "total": len(items)})

@router.get("/training-history")
def list_training_history(
    request: Request,
    query: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    logs = db.execute(
        select(AuditLog)
        .where(AuditLog.action_type == "bot.version.nlu.train")
        .order_by(AuditLog.created_at.desc())
    ).scalars().all()
    version_ids = [log.target_id for log in logs if log.target_id is not None]
    versions = {
        version.id: version
        for version in db.execute(
            select(BotVersion).where(BotVersion.id.in_(version_ids), BotVersion.deleted_at.is_(None))
        ).scalars().all()
    }
    fallback_versions = db.execute(
        select(BotVersion).where(BotVersion.deleted_at.is_(None))
    ).scalars().all()
    trained_fallback_versions = [
        version
        for version in fallback_versions
        if version.id not in versions and _version_training_state(version)
    ]
    all_versions = {**versions, **{version.id: version for version in trained_fallback_versions}}
    bots = {
        bot.id: bot
        for bot in db.execute(
            select(Bot).where(Bot.id.in_([version.bot_id for version in all_versions.values()]), Bot.deleted_at.is_(None))
        ).scalars().all()
    }
    group_map = _get_group_name_map(db, [bot.group_id for bot in bots.values() if bot.group_id is not None])
    actor_names = _actor_name_map(db, {log.actor_user_id for log in logs if log.actor_user_id is not None})

    items: list[AdminTrainingHistoryItem] = []
    represented_version_ids: set[UUID] = set()
    for log in logs:
        if log.target_id is None or log.target_id not in versions:
            continue
        version = versions[log.target_id]
        bot = bots.get(version.bot_id)
        if bot is None:
            continue
        represented_version_ids.add(version.id)
        group = group_map.get(bot.group_id) if bot.group_id is not None else None
        after_json = log.after_json or {}
        counts = _safe_record(after_json.get("counts")) if isinstance(after_json, dict) else {}
        document_count = int(counts.get("intent_documents") or 0) + int(counts.get("entity_documents") or 0)
        started_at = _parse_datetime(after_json.get("started_at")) if isinstance(after_json, dict) else None
        completed_at = _parse_datetime(after_json.get("completed_at")) if isinstance(after_json, dict) else None
        row_started_at = started_at or log.created_at
        row_completed_at = _training_completed_at(row_started_at, completed_at or log.created_at)
        engine_snapshot = _training_engine_snapshot(after_json, version, bot)
        items.append(
            AdminTrainingHistoryItem(
                id=log.id,
                group_name=group.name if group is not None else "-",
                bot_name=bot.name,
                version_no=version.version_no,
                nlu_type=engine_snapshot["nlu_type"],
                nlu_model=engine_snapshot["nlu_model"],
                training_status="학습성공" if document_count > 0 else "학습완료",
                user_login_id=actor_names.get(log.actor_user_id, "SYSTEM") if log.actor_user_id is not None else "SYSTEM",
                started_at=row_started_at,
                completed_at=row_completed_at,
                data_json=_compact_training_history_json(after_json),
            )
        )

    for version in trained_fallback_versions:
        if version.id in represented_version_ids:
            continue
        bot = bots.get(version.bot_id)
        if bot is None:
            continue
        group = group_map.get(bot.group_id) if bot.group_id is not None else None
        training = _version_training_state(version)
        trained_at = _parse_datetime(training.get("trained_at")) or version.updated_at or version.created_at
        started_at = _parse_datetime(training.get("started_at")) or trained_at
        completed_at = _training_completed_at(started_at, _parse_datetime(training.get("completed_at")) or trained_at)
        engine_snapshot = _training_engine_snapshot(training, version, bot)
        items.append(
            AdminTrainingHistoryItem(
                id=uuid5(NAMESPACE_URL, f"aidot-training-history:{version.id}:{trained_at.isoformat()}"),
                group_name=group.name if group is not None else "-",
                bot_name=bot.name,
                version_no=version.version_no,
                nlu_type=engine_snapshot["nlu_type"],
                nlu_model=engine_snapshot["nlu_model"],
                training_status="학습성공" if str(training.get("status") or "").lower() == "success" else "학습완료",
                user_login_id="SYSTEM",
                started_at=started_at,
                completed_at=completed_at,
                data_json=_version_training_history_json(version),
            )
        )

    items.sort(key=lambda item: item.started_at, reverse=True)
    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in item.bot_name.lower()
            or lowered in item.group_name.lower()
            or lowered in item.user_login_id.lower()
        ]

    return success_response(request, {"items": [item.model_dump() for item in items], "total": len(items)})


def _serialize_channel_message_for_admin(message: ChannelMessage) -> dict[str, object]:
    return {
        "id": str(message.id),
        "participant_id": message.participant_id,
        "participant_kind": message.participant_kind,
        "participant_name": message.participant_name,
        "message_type": message.message_type,
        "text": message.text,
        "payload_json": message.payload_json or {},
        "created_at": message.created_at.isoformat(),
    }


def _channel_session_utterances(messages: list[dict[str, object]]) -> list[str]:
    utterances: list[str] = []
    for message in messages:
        if str(message.get("participant_kind") or "").lower() != "user":
            continue
        text = str(message.get("text") or "").strip()
        if text:
            utterances.append(text)
    return utterances


def _channel_room_history(room: ChannelRoom) -> dict[str, object]:
    metadata = _safe_record(room.metadata_json)
    return _safe_record(metadata.get("conversationHistory"))


def _merge_queue_runtime_events(events: list[ChannelQueueEvent | None]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for event in events:
        if event is None:
            continue
        merged.extend(_queue_runtime_events(event.result_json))
    return merged


def _build_channel_session_history_item(
    *,
    room: ChannelRoom,
    room_messages: list[dict[str, object]],
    latest_event: ChannelQueueEvent | None,
    runtime_events: list[dict[str, object]],
    bot: Bot | None,
    version: BotVersion | None,
    group: Group | None,
) -> AdminConversationHistoryItem:
    history = _channel_room_history(room)
    started_at = _parse_datetime(history.get("started_at")) or room.created_at
    utterances = [str(item) for item in history.get("user_utterances") or [] if str(item).strip()]
    raw_utterances = [str(item) for item in history.get("user_raw_utterances") or [] if str(item).strip()]
    transcript = [
        _safe_record(item)
        for item in history.get("transcript") or []
        if isinstance(item, dict)
    ]
    latest_result = _safe_record(latest_event.result_json if latest_event else None)
    first_runtime_event = runtime_events[0] if runtime_events else {}
    last_runtime_event = runtime_events[-1] if runtime_events else {}
    problem_event = _runtime_problem_event(runtime_events)
    conversation_result = _channel_conversation_result(latest_event, room, latest_result)
    latest_intent_name = str(history.get("latest_intent_name") or "").strip()
    start_intent_name = str(history.get("start_intent_name") or "").strip()
    start_module_name = _runtime_intent_name(
        history,
        first_runtime_event,
        *runtime_events,
        latest_result,
        fallback=latest_intent_name or (latest_event.intent_name if latest_event and latest_event.intent_name else "-"),
    )
    return AdminConversationHistoryItem(
        id=f"channel-session:{room.id}",
        group_name=group.name if group else "-",
        channel_name=_channel_display_name(room.channel_type),
        bot_name=bot.name if bot else "-",
        version_no=version.version_no if version else None,
        user_key=room.participant_id or room.client_room_id or "-",
        intent_or_module_name=start_intent_name or start_module_name,
        uttered_at=started_at,
        result=conversation_result,
        data_json={
            "messages": transcript or room_messages,
            "room_id": str(room.id),
            "client_room_id": room.client_room_id,
            "participant_id": room.participant_id,
            "version_no": version.version_no if version else None,
            "queue_event_id": str(history.get("latest_queue_event_id") or latest_event.id) if latest_event or history.get("latest_queue_event_id") else None,
            "result": conversation_result,
            "dialog_ended": history.get("dialog_ended") is True or latest_result.get("dialogEnded") is True,
            "session_ended": history.get("session_ended") is True or latest_result.get("sessionEnded") is True,
            "completion_reason": str(history.get("completion_reason") or latest_result.get("completionReason") or ""),
            "queue_status": str(history.get("latest_queue_status") or (latest_event.status if latest_event else "") or "") or None,
            "room_status": room.status,
            "runtime_events": runtime_events,
            "runtime_summary": _runtime_summary(runtime_events, conversation_result),
            "latest_problem_event": problem_event or {},
            "problem_location": _runtime_event_location(problem_event),
            "session_started_at": started_at.isoformat(),
            "session_message_count": int(history.get("message_count") or len(transcript) or len(room_messages)),
            "session_user_message_count": int(history.get("user_message_count") or len(utterances)),
            "session_user_utterances": utterances,
            "session_user_raw_utterances": raw_utterances,
            "session_first_user_utterance": str(history.get("first_user_utterance") or (utterances[0] if utterances else "")),
            "session_ended_at": history.get("ended_at"),
            "session_end_reason": history.get("session_end_reason"),
            "conversation_history": history,
        },
    )


def _channel_conversation_result(event: ChannelQueueEvent | None, room: ChannelRoom, result_json: dict[str, object]) -> str:
    if event is None:
        return room.status
    if event.status == "failed" or event.error_message:
        return "실패"
    if result_json.get("sessionEnded") is True:
        return "세션종료"
    if result_json.get("dialogEnded") is True:
        return "대화종료"
    return event.status


def _simulator_transcript_display_text(message: dict[str, object]) -> str:
    text = str(message.get("text") or "").strip()
    if text:
        return text
    card_title = str(message.get("cardTitle") or "").strip()
    if card_title:
        return card_title
    template = _safe_record(message.get("template"))
    template_title = str(template.get("title") or "").strip()
    template_lines = [str(item).strip() for item in template.get("lines") or [] if str(item).strip()]
    template_actions = [str(item).strip() for item in template.get("actions") or [] if str(item).strip()]
    parts = [
        template_title or ("RichForm 카드" if str(template.get("kind") or "").strip().lower() == "rich-form" else ""),
        template_lines[0] if template_lines else "",
        ", ".join(template_actions[:3]) if template_actions else "",
    ]
    display_text = " / ".join(part for part in parts if part)
    if display_text:
        return display_text
    table = _safe_record(message.get("table"))
    if table:
        return "테이블 카드"
    quick_replies = [str(item).strip() for item in message.get("quickReplies") or [] if str(item).strip()]
    if quick_replies:
        return f"선택지: {', '.join(quick_replies[:3])}"
    return "메시지"


def _is_internal_simulator_utterance(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    normalized = text.lower()
    if normalized in {"end", "start", "-", "종료", "시작"}:
        return True
    try:
        UUID(text)
        return True
    except ValueError:
        return False


def _direct_simulator_user_utterances(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    utterances: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if _is_internal_simulator_utterance(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        utterances.append(text)
    return utterances


def _simulator_start_dialog_name(*sources: object) -> str:
    keys = (
        "startDialogName",
        "start_dialog_name",
        "startDialogId",
        "start_dialog_id",
        "dialogName",
        "dialog_name",
        "intentName",
        "intent_name",
        "moduleName",
        "module_name",
    )
    for source in sources:
        record = _safe_record(source)
        nested_records = [
            record,
            _safe_record(record.get("data")),
            _safe_record(record.get("metadata")),
            _safe_record(record.get("runtime")),
            _safe_record(record.get("detail")),
            _safe_record(record.get("dialog") or record.get("intent")),
        ]
        for nested in nested_records:
            for key in keys:
                value = str(nested.get(key) or "").strip()
                if value and value != "-":
                    return value
    return "-"


@router.get("/conversations")
def list_conversation_history(
    request: Request,
    query: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=ADMIN_HISTORY_DEFAULT_PAGE_SIZE, ge=1, le=ADMIN_HISTORY_MAX_PAGE_SIZE),
    group_name: str | None = Query(default=None),
    bot_name: str | None = Query(default=None),
    channel_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    resolved_page = page if isinstance(page, int) and page > 0 else 1
    resolved_page_size = (
        page_size
        if isinstance(page_size, int) and 1 <= page_size <= ADMIN_HISTORY_MAX_PAGE_SIZE
        else ADMIN_HISTORY_DEFAULT_PAGE_SIZE
    )

    parsed_start = _parse_admin_filter_datetime(start_date if isinstance(start_date, str) else None)
    parsed_end = _parse_admin_filter_datetime(end_date if isinstance(end_date, str) else None, end_of_day=True)
    entries = _read_web_jsonl_logs(
        "simulator",
        "simulator",
        start_at=parsed_start,
        end_at=parsed_end,
    )
    ended_sessions: set[str] = set()
    bot_ids: set[UUID] = set()
    simulator_sessions: dict[str, dict[str, object]] = {}

    for entry in entries:
        payload = _log_payload(entry)
        detail = _log_detail(payload)
        server_time = _parse_datetime(entry.get("serverTime"))
        if server_time is None:
            continue
        if not _datetime_in_range(server_time, start_at=parsed_start, end_at=parsed_end):
            continue
        session_id = str(payload.get("simulatorSessionId") or "")
        if not session_id:
            continue
        bot_id_value = str(payload.get("botId") or "")
        try:
            if bot_id_value:
                bot_ids.add(UUID(bot_id_value))
        except ValueError:
            pass
        log = _safe_record(detail.get("log"))
        if log.get("description") == "대화 흐름이 종료되었습니다." and session_id:
            ended_sessions.add(session_id)
        runtime = _safe_record(payload.get("runtime"))
        event_name = str(payload.get("event") or "")
        session = simulator_sessions.setdefault(
            session_id,
            {
                "id": session_id,
                "bot_id_value": bot_id_value,
                "bot_name": str(payload.get("botName") or "-"),
                "version_no": _version_no_from_value(payload.get("versionId")),
                "uttered_at": server_time,
                "result": "대화종료" if session_id in ended_sessions else "진행중",
                "intent_or_module_name": "-",
                "entries": [],
                "transcript_messages": [],
                "submitted_user_utterances": [],
                "classified_user_utterances": [],
            },
        )
        if server_time < session["uttered_at"]:
            session["uttered_at"] = server_time
        next_result = str(detail.get("resultType") or "").strip()
        if next_result:
            current_result = str(session["result"] or "")
            if _simulator_result_priority(next_result) >= _simulator_result_priority(current_result):
                session["result"] = next_result
        elif session_id in ended_sessions and _simulator_result_priority(session["result"]) == 0:
            session["result"] = "대화종료"
        if str(session["bot_name"] or "-") == "-" and str(payload.get("botName") or "").strip():
            session["bot_name"] = str(payload.get("botName") or "-")
        if session["version_no"] is None:
            session["version_no"] = _version_no_from_value(payload.get("versionId"))
        candidate_intent_or_module_name = _runtime_intent_name(
            payload,
            detail,
            runtime,
            log,
            fallback=_simulator_start_dialog_name(payload, detail, runtime, log),
        )
        session["intent_or_module_name"] = _prefer_start_intent_or_module_name(
            session["intent_or_module_name"],
            candidate_intent_or_module_name,
        )
        utterance = str(
            detail.get("utterance")
            or detail.get("userUtterance")
            or log.get("utterance")
            or log.get("userUtterance")
            or ""
        ).strip()
        if event_name == "simulator.user_message" and utterance:
            session["submitted_user_utterances"].append(utterance)
        elif event_name == "simulator.user_utterance" and utterance:
            session["classified_user_utterances"].append(utterance)
        elif event_name == "simulator.transcript_message":
            transcript_payload = _safe_record(detail.get("message") or payload.get("message"))
            participant_kind = str(
                detail.get("participantKind")
                or payload.get("participantKind")
                or transcript_payload.get("sender")
                or ""
            ).strip().lower()
            if participant_kind in {"user", "bot", "system"}:
                participant_name = str(
                    detail.get("participantName")
                    or payload.get("participantName")
                    or ("사용자" if participant_kind == "user" else str(session["bot_name"]) if participant_kind == "bot" else "시스템")
                ).strip()
                created_at = str(detail.get("createdAt") or payload.get("createdAt") or server_time.isoformat())
                session["transcript_messages"].append(
                    {
                        "id": str(transcript_payload.get("id") or f"{session_id}:{len(session['transcript_messages']) + 1}"),
                        "participant_kind": participant_kind,
                        "participant_name": participant_name or ("사용자" if participant_kind == "user" else str(session["bot_name"]) if participant_kind == "bot" else "시스템"),
                        "text": str(transcript_payload.get("text") or ""),
                        "display_text": _simulator_transcript_display_text(transcript_payload),
                        "payload_json": transcript_payload,
                        "created_at": created_at,
                    }
                )
        session["entries"].append(entry)

    context_map = _bot_group_context_map(db, bot_ids)
    items: list[AdminConversationHistoryItem] = []
    for session in simulator_sessions.values():
        group_name = "-"
        bot_name = str(session["bot_name"])
        bot_id_value = str(session.get("bot_id_value") or "")
        try:
            context = context_map.get(UUID(bot_id_value))
            if context:
                group_name, bot_name = context
        except ValueError:
            pass
        session_messages = list(session["transcript_messages"])
        transcript_user_utterances = [
            str(message.get("display_text") or message.get("text") or "").strip()
            for message in session_messages
            if str(message.get("participant_kind") or "").lower() == "user"
            and str(message.get("display_text") or message.get("text") or "").strip()
        ]
        session_user_utterances = list(
            _direct_simulator_user_utterances(session["submitted_user_utterances"])
            or _direct_simulator_user_utterances(session["classified_user_utterances"])
            or _direct_simulator_user_utterances(transcript_user_utterances)
        )
        if not session_user_utterances:
            continue
        items.append(
            AdminConversationHistoryItem(
                id=f"simulator-session:{session['id']}",
                group_name=group_name,
                channel_name=_channel_display_name("simulator"),
                bot_name=bot_name,
                version_no=session["version_no"],
                user_key=str(session["id"]),
                intent_or_module_name=str(session["intent_or_module_name"]),
                uttered_at=session["uttered_at"],
                result=str(session["result"]),
                data_json={
                    "messages": session_messages,
                    "entries": session["entries"],
                    "session_user_utterances": session_user_utterances,
                    "session_first_user_utterance": session_user_utterances[0] if session_user_utterances else "",
                },
            )
        )

    room_statement = (
        select(ChannelRoom)
        .join(Bot, Bot.id == ChannelRoom.bot_id)
        .where(
            Bot.organization_id == current_user.organization_id,
        )
        .order_by(ChannelRoom.updated_at.desc())
        .limit(500)
    )
    if parsed_start is not None:
        room_statement = room_statement.where(ChannelRoom.created_at >= parsed_start)
    if parsed_end is not None:
        room_statement = room_statement.where(ChannelRoom.created_at <= parsed_end)
    db_rooms = db.scalars(room_statement).all()
    room_ids = {room.id for room in db_rooms}
    room_messages_by_room_id: dict[UUID, list[dict[str, object]]] = {}
    if room_ids:
        ranked_messages = (
            select(
                ChannelMessage.id.label("message_id"),
                func.row_number()
                .over(
                    partition_by=ChannelMessage.room_id,
                    order_by=ChannelMessage.created_at.desc(),
                )
                .label("history_rank"),
            )
            .where(
                ChannelMessage.room_id.in_(room_ids),
                ChannelMessage.deleted_at.is_(None),
            )
            .subquery()
        )
        room_messages = db.scalars(
            select(ChannelMessage)
            .join(ranked_messages, ranked_messages.c.message_id == ChannelMessage.id)
            .where(ranked_messages.c.history_rank <= ADMIN_CONVERSATION_MESSAGES_PER_ROOM)
            .order_by(ChannelMessage.created_at.asc())
        ).all()
        for room_message in room_messages:
            room_messages_by_room_id.setdefault(room_message.room_id, []).append(_serialize_channel_message_for_admin(room_message))
    bot_ids_from_rooms = {room.bot_id for room in db_rooms}
    bots_from_rooms = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(bot_ids_from_rooms))).all()} if bot_ids_from_rooms else {}
    version_ids_from_rooms = {room.bot_version_id for room in db_rooms}
    versions_from_rooms = {version.id: version for version in db.scalars(select(BotVersion).where(BotVersion.id.in_(version_ids_from_rooms))).all()} if version_ids_from_rooms else {}
    group_ids_from_bots = {bot.group_id for bot in bots_from_rooms.values()}
    groups_from_bots = {group.id: group for group in db.scalars(select(Group).where(Group.id.in_(group_ids_from_bots))).all()} if group_ids_from_bots else {}
    queue_events_by_room_id: dict[UUID, list[ChannelQueueEvent]] = {}
    if room_ids:
        ranked_queue_events = (
            select(
                ChannelQueueEvent.id.label("queue_event_id"),
                func.row_number()
                .over(
                    partition_by=ChannelQueueEvent.room_id,
                    order_by=ChannelQueueEvent.created_at.desc(),
                )
                .label("history_rank"),
            )
            .where(ChannelQueueEvent.room_id.in_(room_ids))
            .subquery()
        )
        for event in db.scalars(
            select(ChannelQueueEvent)
            .join(ranked_queue_events, ranked_queue_events.c.queue_event_id == ChannelQueueEvent.id)
            .where(ranked_queue_events.c.history_rank <= ADMIN_CONVERSATION_QUEUE_EVENTS_PER_ROOM)
            .order_by(ChannelQueueEvent.created_at.asc())
        ).all():
            if event.room_id is not None:
                queue_events_by_room_id.setdefault(event.room_id, []).append(event)

    for room in db_rooms:
        history = _channel_room_history(room)
        if int(history.get("user_message_count") or 0) <= 0:
            continue
        bot = bots_from_rooms.get(room.bot_id)
        version = versions_from_rooms.get(room.bot_version_id)
        group = groups_from_bots.get(bot.group_id) if bot else None
        room_messages = room_messages_by_room_id.get(room.id, [])
        ordered_events = queue_events_by_room_id.get(room.id, [])
        latest_event = ordered_events[-1] if ordered_events else None
        runtime_events = _merge_queue_runtime_events(ordered_events)
        items.append(
            _build_channel_session_history_item(
                room=room,
                room_messages=room_messages,
                latest_event=latest_event,
                runtime_events=runtime_events,
                bot=bot,
                version=version,
                group=group,
            )
        )

    items = [
        item
        for item in items
        if _datetime_in_range(item.uttered_at, start_at=parsed_start, end_at=parsed_end)
    ]
    filter_options = {
        "groups": sorted({item.group_name for item in items if item.group_name and item.group_name != "-"}),
        "bots": sorted({item.bot_name for item in items if item.bot_name and item.bot_name != "-"}),
        "channels": sorted({item.channel_name for item in items if item.channel_name and item.channel_name != "-"}),
    }
    if isinstance(group_name, str) and group_name.strip():
        items = [item for item in items if item.group_name == group_name.strip()]
    if isinstance(bot_name, str) and bot_name.strip():
        items = [item for item in items if item.bot_name == bot_name.strip()]
    if isinstance(channel_name, str) and channel_name.strip():
        items = [item for item in items if item.channel_name.lower() == channel_name.strip().lower()]
    items.sort(key=lambda item: item.uttered_at, reverse=True)
    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in _search_blob(
                item.user_key,
                item.intent_or_module_name,
                item.bot_name,
                item.group_name,
                item.channel_name,
                item.result,
                item.data_json,
            )
        ]
    total = len(items)
    page_start = (resolved_page - 1) * resolved_page_size
    page_items = items[page_start:page_start + resolved_page_size]
    return success_response(
        request,
        {
            "items": [item.model_dump() for item in page_items],
            "total": total,
            "page": resolved_page,
            "page_size": resolved_page_size,
            "filter_options": filter_options,
            "message_limit_per_room": ADMIN_CONVERSATION_MESSAGES_PER_ROOM,
            "queue_event_limit_per_room": ADMIN_CONVERSATION_QUEUE_EVENTS_PER_ROOM,
        },
    )


@router.get("/api-call-history")
def list_api_call_history(
    request: Request,
    query: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    simulator_entries = _read_web_jsonl_logs("simulator", "simulator")
    function_entries = _read_web_jsonl_logs("api-function", "api-function")
    latest_url_by_method: dict[str, str] = {}
    latest_http_method_by_method: dict[str, str] = {}
    latest_api_name_by_method: dict[str, str] = {}
    for entry in function_entries:
        payload = _log_payload(entry)
        method_id = str(payload.get("methodId") or "")
        request_url = str(payload.get("requestUrl") or "")
        http_method = str(payload.get("httpMethod") or "")
        api_name = str(payload.get("apiName") or "")
        if method_id and request_url and method_id not in latest_url_by_method:
            latest_url_by_method[method_id] = request_url
        if method_id and http_method and method_id not in latest_http_method_by_method:
            latest_http_method_by_method[method_id] = http_method
        if method_id and api_name and method_id not in latest_api_name_by_method:
            latest_api_name_by_method[method_id] = api_name

    bot_ids: set[UUID] = set()
    rows: list[tuple[dict[str, object], dict[str, object], dict[str, object], datetime]] = []
    for entry in simulator_entries:
        payload = _log_payload(entry)
        event = payload.get("event")
        if event not in {"simulator.function_call_success", "simulator.function_call_failed", "simulator.function_call_exception"}:
            continue
        server_time = _parse_datetime(entry.get("serverTime"))
        if server_time is None:
            continue
        detail = _log_detail(payload)
        bot_id_value = str(payload.get("botId") or "")
        try:
            if bot_id_value:
                bot_ids.add(UUID(bot_id_value))
        except ValueError:
            pass
        rows.append((entry, payload, detail, server_time))

    context_map = _bot_group_context_map(db, bot_ids)
    items: list[AdminApiCallHistoryItem] = []
    for entry, payload, detail, server_time in rows:
        bot_id_value = str(payload.get("botId") or "")
        group_name = "-"
        bot_name = str(payload.get("botName") or "-")
        try:
            context = context_map.get(UUID(bot_id_value))
            if context:
                group_name, bot_name = context
        except ValueError:
            pass
        method_id = str(detail.get("methodId") or "")
        status_code = detail.get("status")
        response_code = f"{status_code} Success" if payload.get("event") == "simulator.function_call_success" else str(status_code or "Error")
        runtime = _safe_record(payload.get("runtime"))
        items.append(
            AdminApiCallHistoryItem(
                id=f"{payload.get('simulatorSessionId') or '-'}:{detail.get('analysisId') or server_time.isoformat()}:{method_id}",
                method=str(detail.get("httpMethod") or latest_http_method_by_method.get(method_id) or "-"),
                filters="log",
                api_name=str(detail.get("apiName") or latest_api_name_by_method.get(method_id) or "-"),
                api_type="USER",
                url=latest_url_by_method.get(method_id, "-"),
                transfer_type="동기",
                channel_name=_channel_display_name("simulator"),
                group_name=group_name,
                bot_name=bot_name,
                version_no=_version_no_from_value(payload.get("versionId")),
                intent_name=_runtime_intent_name(runtime, detail),
                response_code=response_code,
                user_key=str(payload.get("simulatorSessionId") or "-"),
                called_at=server_time,
                data_json={"entry": entry},
            )
        )

    queue_events = db.scalars(
        select(ChannelQueueEvent)
        .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        .where(
            Bot.organization_id == current_user.organization_id,
            ChannelQueueEvent.deleted_at.is_(None),
        )
        .order_by(ChannelQueueEvent.created_at.desc())
        .limit(500)
    ).all()
    queue_bot_ids = {event.bot_id for event in queue_events}
    queue_version_ids = {event.bot_version_id for event in queue_events}
    queue_room_ids = {event.room_id for event in queue_events if event.room_id is not None}
    queue_bots = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(queue_bot_ids))).all()} if queue_bot_ids else {}
    queue_versions = {version.id: version for version in db.scalars(select(BotVersion).where(BotVersion.id.in_(queue_version_ids))).all()} if queue_version_ids else {}
    queue_rooms = {room.id: room for room in db.scalars(select(ChannelRoom).where(ChannelRoom.id.in_(queue_room_ids))).all()} if queue_room_ids else {}
    queue_group_ids = {bot.group_id for bot in queue_bots.values()}
    queue_groups = {group.id: group for group in db.scalars(select(Group).where(Group.id.in_(queue_group_ids))).all()} if queue_group_ids else {}
    for event in queue_events:
        bot = queue_bots.get(event.bot_id)
        version = queue_versions.get(event.bot_version_id)
        room = queue_rooms.get(event.room_id) if event.room_id else None
        group = queue_groups.get(bot.group_id) if bot else None
        queue_payload = _queue_event_payload(event)
        queue_result = _safe_record(event.result_json)
        for runtime_event in _queue_runtime_events(event.result_json):
            event_name = str(runtime_event.get("event") or "")
            if not event_name.startswith("channel.runtime.function_"):
                continue
            detail = _safe_record(runtime_event.get("data"))
            status_code = detail.get("status")
            ok = detail.get("ok") is True and str(runtime_event.get("level") or "").lower() != "error"
            items.append(
                AdminApiCallHistoryItem(
                    id=f"channel:{event.id}:{runtime_event.get('time') or len(items)}",
                    method=str(detail.get("httpMethod") or "-"),
                    filters="channel-runtime",
                    api_name=str(detail.get("apiName") or detail.get("apiId") or "-"),
                    api_type="USER",
                    url=str(detail.get("url") or "-"),
                    transfer_type="동기",
                    channel_name=_channel_display_name(event.channel_type),
                    group_name=group.name if group else "-",
                    bot_name=bot.name if bot else "-",
                    version_no=version.version_no if version else 0,
                    intent_name=event.intent_name or _runtime_intent_name(runtime_event, detail),
                    response_code=f"{status_code or '-'} {'Success' if ok else 'Error'}",
                    user_key=(room.participant_id if room else event.participant_id) or "-",
                    called_at=_parse_datetime(str(runtime_event.get("time") or "")) or event.status_changed_at,
                    data_json={
                        "queue_event_id": str(event.id),
                        "channel_type": event.channel_type,
                        "room_id": str(event.room_id) if event.room_id else None,
                        "bot_id": str(event.bot_id),
                        "version_id": str(event.bot_version_id),
                        "queue_status": event.status,
                        "queue_error_message": event.error_message,
                        "completion_reason": str(queue_result.get("completionReason") or ""),
                        "dialog_ended": queue_result.get("dialogEnded") is True,
                        "session_ended": queue_result.get("sessionEnded") is True,
                        "runtime_event": runtime_event,
                        "problem_location": _runtime_event_location(runtime_event),
                        "queue_payload": queue_payload,
                        "queue_result": queue_result,
                    },
                )
            )

    items.sort(key=lambda item: item.called_at, reverse=True)
    if query:
        lowered = query.strip().lower()
        items = [
            item
            for item in items
            if lowered in _search_blob(
                item.api_name,
                item.intent_name,
                item.bot_name,
                item.group_name,
                item.method,
                item.url,
                item.response_code,
                item.user_key,
                item.data_json,
            )
        ]
    return success_response(request, {"items": [item.model_dump() for item in items], "total": len(items)})


@router.get("/queue-history")
def list_queue_history(
    request: Request,
    query: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=ADMIN_HISTORY_DEFAULT_PAGE_SIZE, ge=1, le=ADMIN_HISTORY_MAX_PAGE_SIZE),
    group_name: str | None = Query(default=None),
    bot_name: str | None = Query(default=None),
    channel_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)
    resolved_page = page if isinstance(page, int) and page > 0 else 1
    resolved_page_size = (
        page_size
        if isinstance(page_size, int) and 1 <= page_size <= ADMIN_HISTORY_MAX_PAGE_SIZE
        else ADMIN_HISTORY_DEFAULT_PAGE_SIZE
    )
    parsed_start = _parse_admin_filter_datetime(start_date if isinstance(start_date, str) else None)
    parsed_end = _parse_admin_filter_datetime(end_date if isinstance(end_date, str) else None, end_of_day=True)
    execute = getattr(db, "execute", None)
    available_filters: list[object] = []
    if callable(execute):
        available_filters = list(
            execute(
                select(Group.name, Bot.name, ChannelQueueEvent.channel_type)
                .select_from(ChannelQueueEvent)
                .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
                .join(Group, Group.id == Bot.group_id)
                .where(
                    Bot.organization_id == current_user.organization_id,
                    ChannelQueueEvent.deleted_at.is_(None),
                )
                .distinct()
            ).all()
        )
    filter_options = {
        "groups": sorted({str(row[0]) for row in available_filters if row[0]}),
        "bots": sorted({str(row[1]) for row in available_filters if row[1]}),
        "channels": sorted({_channel_display_name(str(row[2])) for row in available_filters if row[2]}),
    }
    event_statement = (
        select(ChannelQueueEvent)
        .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        .join(Group, Group.id == Bot.group_id)
        .where(
            Bot.organization_id == current_user.organization_id,
            ChannelQueueEvent.deleted_at.is_(None),
        )
        .order_by(ChannelQueueEvent.created_at.desc())
    )
    if parsed_start is not None:
        event_statement = event_statement.where(ChannelQueueEvent.created_at >= parsed_start)
    if parsed_end is not None:
        event_statement = event_statement.where(ChannelQueueEvent.created_at <= parsed_end)
    if isinstance(group_name, str) and group_name.strip():
        event_statement = event_statement.where(Group.name == group_name.strip())
    if isinstance(bot_name, str) and bot_name.strip():
        event_statement = event_statement.where(Bot.name == bot_name.strip())
    if isinstance(channel_name, str) and channel_name.strip():
        event_statement = event_statement.where(
            func.lower(ChannelQueueEvent.channel_type) == channel_name.strip().lower()
        )
    normalized_query = query.strip() if isinstance(query, str) else ""
    if normalized_query:
        search_pattern = f"%{normalized_query}%"
        event_statement = event_statement.where(
            or_(
                ChannelQueueEvent.intent_name.ilike(search_pattern),
                ChannelQueueEvent.sender_system.ilike(search_pattern),
                ChannelQueueEvent.channel_type.ilike(search_pattern),
                ChannelQueueEvent.receiver.ilike(search_pattern),
                ChannelQueueEvent.receive_status.ilike(search_pattern),
                ChannelQueueEvent.status.ilike(search_pattern),
                Bot.name.ilike(search_pattern),
                Group.name.ilike(search_pattern),
                cast(ChannelQueueEvent.parameter_json, String).ilike(search_pattern),
                cast(ChannelQueueEvent.result_json, String).ilike(search_pattern),
            )
        )
    scalar_count = getattr(db, "scalar", None)
    total = (
        int(scalar_count(select(func.count()).select_from(event_statement.order_by(None).subquery())) or 0)
        if callable(scalar_count)
        else None
    )
    events = db.scalars(
        event_statement
        .offset((resolved_page - 1) * resolved_page_size)
        .limit(resolved_page_size)
    ).all()
    bot_ids = {event.bot_id for event in events}
    version_ids = {event.bot_version_id for event in events}
    room_ids = {event.room_id for event in events if event.room_id is not None}
    bots = {
        bot.id: bot
        for bot in db.scalars(select(Bot).where(Bot.id.in_(bot_ids))).all()
    } if bot_ids else {}
    versions = {
        version.id: version
        for version in db.scalars(select(BotVersion).where(BotVersion.id.in_(version_ids))).all()
    } if version_ids else {}
    rooms = {
        room.id: room
        for room in db.scalars(select(ChannelRoom).where(ChannelRoom.id.in_(room_ids))).all()
    } if room_ids else {}
    group_ids = {bot.group_id for bot in bots.values()}
    groups = {
        group.id: group
        for group in db.scalars(select(Group).where(Group.id.in_(group_ids))).all()
    } if group_ids else {}

    rows: list[AdminQueueHistoryItem] = []
    for event in events:
        bot = bots.get(event.bot_id)
        version = versions.get(event.bot_version_id)
        room = rooms.get(event.room_id) if event.room_id else None
        group = groups.get(bot.group_id) if bot else None
        parameter = json.dumps(event.parameter_json or {}, ensure_ascii=False)
        runtime_events = _queue_runtime_events(event.result_json)
        problem_event = _runtime_problem_event(runtime_events)
        result = {
            **(event.result_json or {}),
            "queue_status": event.status,
            "error_message": event.error_message,
            "version_no": version.version_no if version else None,
            "room_id": str(event.room_id) if event.room_id else None,
            "request_message_id": str(event.request_message_id) if event.request_message_id else None,
            "runtime_summary": _runtime_summary(runtime_events, event.error_message or event.status),
            "latest_problem_event": problem_event or {},
        }
        rows.append(
            AdminQueueHistoryItem(
                id=str(event.id),
                intent_name=event.intent_name or _runtime_intent_name(
                    runtime_events[-1] if runtime_events else {},
                    problem_event,
                    event.result_json,
                ),
                sender_system=event.sender_system,
                priority=event.priority,
                parameter=parameter,
                channel_name=_channel_display_name(event.channel_type),
                bot_name=bot.name if bot else "-",
                receiver=event.receiver,
                receive_status=event.receive_status,
                requested_at=event.created_at,
                status_changed_at=event.status_changed_at,
                data_json={
                    "group_name": group.name if group else "-",
                    "participant_id": event.participant_id,
                    "room_status": room.status if room else None,
                    **result,
                },
            )
        )
    if not available_filters:
        filter_options = {
            "groups": sorted({str(item.data_json.get("group_name")) for item in rows if item.data_json.get("group_name") not in {None, "-", ""}}),
            "bots": sorted({item.bot_name for item in rows if item.bot_name and item.bot_name != "-"}),
            "channels": sorted({item.channel_name for item in rows if item.channel_name and item.channel_name != "-"}),
        }
    if total is None:
        total = len(rows)
        rows = rows[:resolved_page_size]
    return success_response(
        request,
        {
            "items": [item.model_dump() for item in rows],
            "total": total,
            "page": resolved_page,
            "page_size": resolved_page_size,
            "filter_options": filter_options,
            "source": "channel_queue_events",
        },
    )


@router.post("/queue/process")
def process_admin_queue_events(
    request: Request,
    channel_type: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    results = _process_queued_channel_events(
        db,
        request,
        channel=channel_type.strip().lower() if channel_type else None,
        organization_id=current_user.organization_id,
        limit=limit,
    )
    db.commit()
    return success_response(
        request,
        {
            "processed": len(results),
            "items": results,
        },
    )


@router.get("/intent-feedback")
def list_intent_feedback(
    request: Request,
    query: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)
    events = db.scalars(
        select(ChannelQueueEvent)
        .join(Bot, Bot.id == ChannelQueueEvent.bot_id)
        .where(
            Bot.organization_id == current_user.organization_id,
            ChannelQueueEvent.deleted_at.is_(None),
        )
        .order_by(ChannelQueueEvent.created_at.desc())
        .limit(1000)
    ).all()
    if not events:
        return success_response(request, {"items": [], "total": 0, "source": "channel_queue_events"})

    bot_ids = {event.bot_id for event in events}
    version_ids = {event.bot_version_id for event in events}
    message_ids = {event.request_message_id for event in events if event.request_message_id is not None}
    bots = {bot.id: bot for bot in db.scalars(select(Bot).where(Bot.id.in_(bot_ids))).all()} if bot_ids else {}
    versions = {version.id: version for version in db.scalars(select(BotVersion).where(BotVersion.id.in_(version_ids))).all()} if version_ids else {}
    messages = {message.id: message for message in db.scalars(select(ChannelMessage).where(ChannelMessage.id.in_(message_ids))).all()} if message_ids else {}
    group_ids = {bot.group_id for bot in bots.values()}
    groups = {group.id: group for group in db.scalars(select(Group).where(Group.id.in_(group_ids))).all()} if group_ids else {}
    version_quality_diagnostics: dict[object, dict[str, object]] = {}
    for version in versions.values():
        version_json = _safe_record(version.version_json)
        system_config = _safe_record(version_json.get("system_config"))
        evaluation = _safe_record(system_config.get("nlu_evaluation"))
        latest = _safe_record(evaluation.get("latest"))
        diagnostics = _safe_record(evaluation.get("quality_diagnostics")) or _safe_record(latest.get("quality_diagnostics"))
        if diagnostics:
            version_quality_diagnostics[version.id] = diagnostics

    buckets: dict[str, dict[str, object]] = {}
    for event in events:
        result = _safe_record(event.result_json)
        runtime_events = _queue_runtime_events(result)
        intent_runtime_event = next(
            (
                runtime_event
                for runtime_event in runtime_events
                if str(runtime_event.get("event") or "") in {"channel.runtime.intent_matched", "channel.runtime.intent_fallback"}
            ),
            {},
        )
        runtime_data = _safe_record(intent_runtime_event.get("data"))
        intent_name = event.intent_name or str(runtime_data.get("intentName") or "")
        if not intent_name:
            intent_name = "의도 미분류"
        score_value = result.get("intentScore", runtime_data.get("intentScore", 0))
        try:
            score = float(score_value or 0)
        except (TypeError, ValueError):
            score = 0.0
        event_name = str(intent_runtime_event.get("event") or "")
        is_fallback = intent_name == "의도 미분류" or event_name == "channel.runtime.intent_fallback" or result.get("intentId") is None
        is_low_score = score < 50
        if not is_fallback and not is_low_score:
            continue

        bot = bots.get(event.bot_id)
        version = versions.get(event.bot_version_id)
        group = groups.get(bot.group_id) if bot else None
        message = messages.get(event.request_message_id) if event.request_message_id else None
        bucket_key = f"{group.id if group else '-'}:{event.bot_id}:{event.bot_version_id}:{event.channel_type}:{intent_name}"
        bucket = buckets.setdefault(
            bucket_key,
            {
                "id": bucket_key,
                "group_name": group.name if group else "-",
                "bot_name": bot.name if bot else "-",
                "version_id": event.bot_version_id,
                "version_no": version.version_no if version else 0,
                "channel_name": _channel_display_name(event.channel_type),
                "intent_name": intent_name,
                "scores": [],
                "fallback_count": 0,
                "low_score_count": 0,
                "samples": [],
            },
        )
        scores = bucket["scores"]
        if isinstance(scores, list):
            scores.append(score)
        if is_fallback:
            bucket["fallback_count"] = int(bucket.get("fallback_count") or 0) + 1
        if is_low_score:
            bucket["low_score_count"] = int(bucket.get("low_score_count") or 0) + 1
        samples = bucket["samples"]
        if isinstance(samples, list) and len(samples) < 20:
            samples.append(
                {
                    "utterance": message.text if message else str(runtime_data.get("messagePreview") or ""),
                    "score": score,
                    "event": event_name or "channel.runtime.intent_feedback",
                    "queue_event_id": str(event.id),
                    "occurred_at": event.status_changed_at.isoformat(),
                    "runtime_event": intent_runtime_event,
                }
            )

    rows: list[AdminIntentFeedbackItem] = []
    for bucket in buckets.values():
        scores = [float(value) for value in bucket.get("scores", []) if isinstance(value, (int, float))]
        average_score = round(sum(scores) / len(scores), 2) if scores else 0.0
        feedback_count = len(scores)
        samples = bucket.get("samples") if isinstance(bucket.get("samples"), list) else []
        fallback_count = int(bucket.get("fallback_count") or 0)
        low_score_count = int(bucket.get("low_score_count") or 0)
        quality_diagnostics = _safe_record(version_quality_diagnostics.get(bucket.get("version_id")))
        quality_items = quality_diagnostics.get("items")
        related_quality_items: list[dict[str, object]] = []
        if isinstance(quality_items, list):
            intent_name = str(bucket.get("intent_name") or "")
            for item in quality_items:
                if not isinstance(item, dict):
                    continue
                expected_name = str(item.get("expected_name") or "")
                predicted_name = str(item.get("predicted_name") or "")
                if intent_name and intent_name not in {expected_name, predicted_name}:
                    continue
                related_quality_items.append(item)
                if len(related_quality_items) >= 10:
                    break
        sample_utterances: list[str] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            utterance = str(sample.get("utterance") or "").strip()
            if utterance and utterance not in sample_utterances:
                sample_utterances.append(utterance)
        diagnosis_reasons: list[str] = []
        if fallback_count > 0:
            diagnosis_reasons.append("미분류로 fallback 답변이 사용된 발화가 있습니다.")
        if low_score_count > 0:
            diagnosis_reasons.append("의도 인식 점수가 기준치보다 낮은 발화가 있습니다.")
        if feedback_count >= 3:
            diagnosis_reasons.append("같은 의도/채널에서 반복적으로 후보가 수집되었습니다.")
        if related_quality_items:
            diagnosis_reasons.append("학습 품질 진단에도 같은 의도의 Feature/Score 보완 후보가 있습니다.")
        score_distribution = {
            "min": round(min(scores), 2) if scores else 0.0,
            "max": round(max(scores), 2) if scores else 0.0,
            "average": average_score,
        }
        recommendation = "학습문장 추가 검토"
        if fallback_count > 0:
            recommendation = "미분류 발화를 학습문장으로 추가 검토"
        elif low_score_count > 0:
            recommendation = "유사 의도와 Feature/Score 비교 필요"
        rows.append(
            AdminIntentFeedbackItem(
                id=str(bucket.get("id") or uuid4()),
                group_name=str(bucket.get("group_name") or "-"),
                bot_name=str(bucket.get("bot_name") or "-"),
                version_no=int(bucket.get("version_no") or 0),
                channel_name=str(bucket.get("channel_name") or "-"),
                intent_name=str(bucket.get("intent_name") or "-"),
                average_score=average_score,
                feedback_count=feedback_count,
                data_json={
                    "source": "channel_queue_events",
                    "fallback_count": fallback_count,
                    "low_score_count": low_score_count,
                    "recommendation": recommendation,
                    "diagnosis_reasons": diagnosis_reasons,
                    "score_distribution": score_distribution,
                    "training_quality_summary": _safe_record(quality_diagnostics.get("summary")),
                    "training_quality_settings": _safe_record(quality_diagnostics.get("settings")),
                    "related_quality_diagnostics": related_quality_items,
                    "suggested_training_sentences": sample_utterances[:10],
                    "samples": samples,
                },
            )
        )

    rows.sort(key=lambda item: (item.feedback_count, 100 - item.average_score), reverse=True)
    if query:
        lowered = query.strip().lower()
        rows = [
            item
            for item in rows
            if lowered in _search_blob(
                item.group_name,
                item.bot_name,
                item.channel_name,
                item.intent_name,
                item.data_json,
            )
        ]
    return success_response(request, {"items": [item.model_dump() for item in rows], "total": len(rows), "source": "channel_queue_events"})


def _build_group_code(existing_codes: list[str]) -> str:
    numbers = []
    for code in existing_codes:
        if code.startswith("RSH") and code[3:].isdigit():
            numbers.append(int(code[3:]))
    next_number = (max(numbers) + 1) if numbers else 0
    return f"RSH{next_number:07d}"


@router.get("/groups")
def list_groups(
    request: Request,
    query: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    organization = _get_default_organization(db)
    groups = db.execute(
        select(Group)
        .where(Group.organization_id == organization.id, Group.deleted_at.is_(None))
        .order_by(Group.created_at.asc())
    ).scalars().all()

    if query:
        lowered = query.strip().lower()
        groups = [
            group
            for group in groups
            if lowered in group.code.lower() or lowered in group.name.lower()
        ]

    group_ids = [group.id for group in groups]
    group_logs = db.execute(
        select(AuditLog)
        .where(AuditLog.target_type == "group", AuditLog.target_id.in_(group_ids))
        .order_by(AuditLog.created_at.asc())
    ).scalars().all()
    actor_names = _actor_name_map(
        db,
        {log.actor_user_id for log in group_logs if log.actor_user_id is not None},
    )

    creator_map: dict[UUID, str] = {group.id: "SYSTEM" for group in groups}
    updater_map: dict[UUID, str] = {group.id: "SYSTEM" for group in groups}
    for log in group_logs:
        if log.target_id is None:
            continue
        actor_name = actor_names.get(log.actor_user_id, "SYSTEM") if log.actor_user_id is not None else "SYSTEM"
        if creator_map.get(log.target_id) == "SYSTEM":
            creator_map[log.target_id] = actor_name
        updater_map[log.target_id] = actor_name

    items = [
        AdminGroupListItem(
            id=group.id,
            code=group.code,
            name=group.name,
            status="사용" if group.status == "active" else "미사용",
            creator_name=creator_map.get(group.id, "SYSTEM"),
            updater_name=updater_map.get(group.id, "SYSTEM"),
            updated_at=group.updated_at,
        )
        for group in groups
    ]

    return success_response(
        request,
        {
            "items": [item.model_dump() for item in items],
            "total": len(items),
        },
    )


@router.get("/groups/{group_id}")
def get_group_detail(
    group_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    group = _get_group_or_404(db, group_id)
    group_logs = db.execute(
        select(AuditLog)
        .where(AuditLog.target_type == "group", AuditLog.target_id == group.id)
        .order_by(AuditLog.created_at.asc())
    ).scalars().all()
    actor_names = _actor_name_map(
        db,
        {log.actor_user_id for log in group_logs if log.actor_user_id is not None},
    )

    creator_name = "SYSTEM"
    updater_name = "SYSTEM"
    for log in group_logs:
        actor_name = actor_names.get(log.actor_user_id, "SYSTEM") if log.actor_user_id is not None else "SYSTEM"
        if creator_name == "SYSTEM":
            creator_name = actor_name
        updater_name = actor_name

    user_count = db.execute(
        select(User).where(
            User.group_id == group.id,
            User.deleted_at.is_(None),
        )
    ).scalars().all()

    detail = AdminGroupDetail(
        id=group.id,
        code=group.code,
        name=group.name,
        status=group.status,  # type: ignore[arg-type]
        status_label="사용" if group.status == "active" else "미사용",
        creator_name=creator_name,
        updater_name=updater_name,
        created_at=group.created_at,
        updated_at=group.updated_at,
        user_count=len(user_count),
        data_json=group.data_json or {},
    )
    return success_response(request, detail.model_dump())


@router.post("/groups")
def create_group(
    payload: GroupCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    existing_codes = db.execute(
        select(Group.code).where(Group.organization_id == organization.id)
    ).scalars().all()
    group = Group(
        organization_id=organization.id,
        code=_build_group_code(existing_codes),
        name=payload.name.strip(),
        status="active",
        data_json={},
    )
    db.add(group)
    db.flush()
    group.data_json = _build_group_json(group, organization)

    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.group.create",
            target_type="group",
            target_id=group.id,
            after_json={"code": group.code, "name": group.name},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    return success_response(
        request,
        {
            "message": "그룹이 생성되었습니다.",
            "group": {
                "id": str(group.id),
                "code": group.code,
                "name": group.name,
            },
        },
    )


@router.patch("/groups/{group_id}")
def update_group(
    group_id: UUID,
    payload: GroupUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    group = _get_group_or_404(db, group_id)
    before_state = {"name": group.name, "status": group.status}
    group.name = payload.name.strip()
    group.status = payload.status
    organization = db.scalar(select(Organization).where(Organization.id == group.organization_id))
    group.data_json = _build_group_json(group, organization)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.group.update",
            target_type="group",
            target_id=group.id,
            before_json=before_state,
            after_json={"name": group.name, "status": group.status},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.add(group)
    db.commit()

    return success_response(request, {"message": "그룹 정보가 변경되었습니다."})


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    group = _get_group_or_404(db, group_id)

    active_users = db.execute(
        select(User).where(User.group_id == group.id, User.deleted_at.is_(None))
    ).scalars().all()
    if active_users:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="삭제하려는 그룹에 사용자가 속해 있으면 삭제할 수 없습니다.",
        )

    pending_signup_requests = db.execute(
        select(SignupRequest).where(
            SignupRequest.group_id == group.id,
            SignupRequest.status == "pending",
        )
    ).scalars().all()
    if pending_signup_requests:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="삭제하려는 그룹에 승인 대기 중인 가입신청이 있으면 삭제할 수 없습니다.",
        )

    linked_bots = db.execute(
        select(Bot).where(
            Bot.group_id == group.id,
            Bot.deleted_at.is_(None),
        )
    ).scalars().all()
    if linked_bots:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="삭제하려는 그룹에 연결된 봇이 있으면 삭제할 수 없습니다.",
        )

    group.deleted_at = datetime.now(timezone.utc)
    db.add(group)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.group.delete",
            target_type="group",
            target_id=group.id,
            before_json={"code": group.code, "name": group.name},
            after_json={"deleted": True},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "그룹이 삭제되었습니다."})


@router.get("/channels")
def list_admin_channels(
    request: Request,
    query: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)

    organization = _get_default_organization(db)
    _ensure_default_admin_channels(db, organization)
    db.commit()

    channels = db.execute(
        select(AdminChannel)
        .where(
            AdminChannel.organization_id == organization.id,
            AdminChannel.deleted_at.is_(None),
        )
        .order_by(AdminChannel.created_at.asc())
    ).scalars().all()

    if query:
        lowered = query.strip().lower()
        channels = [
            channel
            for channel in channels
            if lowered in channel.code.lower()
            or lowered in channel.name.lower()
            or lowered in (channel.description or "").lower()
        ]
    if status_filter in {"active", "inactive"}:
        channels = [channel for channel in channels if channel.status == status_filter]

    actor_names = _actor_name_map(
        db,
        {
            actor_id
            for channel in channels
            for actor_id in (channel.created_by, channel.updated_by)
            if actor_id is not None
        },
    )
    items = [
        _serialize_admin_channel(
            channel,
            actor_names.get(channel.created_by, "SYSTEM") if channel.created_by is not None else "SYSTEM",
            actor_names.get(channel.updated_by, "SYSTEM") if channel.updated_by is not None else "SYSTEM",
        )
        for channel in channels
    ]

    return success_response(
        request,
        {
            "items": [item.model_dump() for item in items],
            "total": len(items),
        },
    )


@router.post("/channels")
def create_admin_channel(
    payload: ChannelCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    code = payload.code.strip().upper()
    existing = db.scalar(
        select(AdminChannel).where(
            AdminChannel.organization_id == organization.id,
            AdminChannel.code == code,
        )
    )
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 존재하는 채널 아이디입니다.")

    channel_config = _build_channel_config(
        payload.provider,
        payload.renderer_type,
        payload.endpoint_url,
        payload.auth_type,
        payload.auth_config,
    )

    if existing is None:
        channel = AdminChannel(
            organization_id=organization.id,
            code=code,
            name=payload.name.strip(),
            description=payload.description.strip() if payload.description else None,
            status=payload.status,
            created_by=current_user.id,
            updated_by=current_user.id,
            data_json=channel_config,
        )
    else:
        channel = existing
        channel.name = payload.name.strip()
        channel.description = payload.description.strip() if payload.description else None
        channel.status = payload.status
        channel.data_json = channel_config
        channel.deleted_at = None
        channel.updated_by = current_user.id

    db.add(channel)
    db.flush()
    channel.data_json = _build_channel_json(channel, current_user.login_id, current_user.login_id)
    db.add(channel)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.channel.create",
            target_type="channel",
            target_id=channel.id,
            after_json=channel.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "채널이 저장되었습니다.", "id": str(channel.id)})


@router.post("/channels/{channel_id}/test")
def test_admin_channel_connection(
    channel_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    channel = db.scalar(
        select(AdminChannel).where(
            AdminChannel.id == channel_id,
            AdminChannel.deleted_at.is_(None),
        )
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널을 찾을 수 없습니다.")

    data_json = dict(channel.data_json or {})
    provider = str(data_json.get("provider") or "webchat")
    renderer_type = str(data_json.get("renderer_type") or "webchat")
    endpoint_url = data_json.get("endpoint_url")
    auth_type = str(data_json.get("auth_type") or "none")
    auth_config = data_json.get("auth_config") if isinstance(data_json.get("auth_config"), dict) else {}
    issues = _channel_connection_issues(
        status_value=channel.status,
        provider=provider,
        renderer_type=renderer_type,
        endpoint_url=endpoint_url if isinstance(endpoint_url, str) else None,
        auth_type=auth_type,
        auth_config=auth_config,
    )

    success = not issues
    result = {
        "success": success,
        "message": "연결 설정이 정상입니다." if success else "연결 설정을 확인해주세요.",
        "issues": issues,
        "provider": provider,
        "renderer_type": renderer_type,
        "endpoint_url": endpoint_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.channel.test",
            target_type="channel",
            target_id=channel.id,
            after_json=result,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, result)

@router.patch("/channels/{channel_id}")
def update_admin_channel(
    channel_id: UUID,
    payload: ChannelUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    channel = db.scalar(
        select(AdminChannel).where(
            AdminChannel.id == channel_id,
            AdminChannel.deleted_at.is_(None),
        )
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널을 찾을 수 없습니다.")

    before_json = channel.data_json or {}
    channel.name = payload.name.strip()
    channel.description = payload.description.strip() if payload.description else None
    channel.status = payload.status
    channel.data_json = _build_channel_config(
        payload.provider,
        payload.renderer_type,
        payload.endpoint_url,
        payload.auth_type,
        payload.auth_config,
    )
    channel.updated_by = current_user.id
    channel.data_json = _build_channel_json(channel, before_json.get("creator_name"), current_user.login_id)
    db.add(channel)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.channel.update",
            target_type="channel",
            target_id=channel.id,
            before_json=before_json,
            after_json=channel.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "채널 정보가 변경되었습니다."})


@router.delete("/channels/{channel_id}")
def delete_admin_channel(
    channel_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    channel = db.scalar(
        select(AdminChannel).where(
            AdminChannel.id == channel_id,
            AdminChannel.deleted_at.is_(None),
        )
    )
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="채널을 찾을 수 없습니다.")

    channel.deleted_at = datetime.now(timezone.utc)
    channel.updated_by = current_user.id
    db.add(channel)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.channel.delete",
            target_type="channel",
            target_id=channel.id,
            before_json=channel.data_json,
            after_json={"deleted": True},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "채널이 삭제되었습니다."})


@router.get("/templates")
def list_admin_templates(
    request: Request,
    query: str | None = Query(default=None),
    channel_code: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    _ensure_default_admin_templates(db, organization)
    db.commit()

    templates = db.execute(
        select(AdminTemplate)
        .where(
            AdminTemplate.organization_id == organization.id,
            AdminTemplate.deleted_at.is_(None),
        )
        .order_by(AdminTemplate.channel_code.asc(), AdminTemplate.created_at.asc())
    ).scalars().all()

    if query:
        lowered = query.strip().lower()
        templates = [
            template
            for template in templates
            if lowered in template.name.lower()
            or lowered in template.channel_code.lower()
            or lowered in template.renderer_type.lower()
            or lowered in template.item_types.lower()
            or lowered in (template.description or "").lower()
        ]
    if channel_code:
        normalized_channel_code = channel_code.strip().upper()
        templates = [template for template in templates if template.channel_code == normalized_channel_code]
    if active_only or status_filter == "active":
        templates = [template for template in templates if template.status == "active"]
    elif status_filter == "inactive":
        templates = [template for template in templates if template.status == "inactive"]

    channel_names = _channel_name_map(db, organization.id)
    actor_names = _actor_name_map(
        db,
        {
            actor_id
            for template in templates
            for actor_id in (template.created_by, template.updated_by)
            if actor_id is not None
        },
    )
    items = [
        _serialize_admin_template(
            template,
            channel_names.get(template.channel_code, template.channel_code),
            actor_names.get(template.created_by, "SYSTEM") if template.created_by is not None else "SYSTEM",
            actor_names.get(template.updated_by, "SYSTEM") if template.updated_by is not None else "SYSTEM",
        )
        for template in templates
    ]
    return success_response(request, {"items": [item.model_dump() for item in items], "total": len(items)})


@router.post("/templates")
def create_admin_template(
    payload: TemplateCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    channel_code = payload.channel_code.strip().upper()
    template_issues = _template_renderer_issues(
        channel_code=channel_code,
        renderer_type=payload.renderer_type,
    )
    if template_issues:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=template_issues[0])
    existing = db.scalar(
        select(AdminTemplate).where(
            AdminTemplate.organization_id == organization.id,
            AdminTemplate.channel_code == channel_code,
            AdminTemplate.name == payload.name.strip(),
        )
    )
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 등록된 템플릿 이름입니다.")

    if existing is None:
        template = AdminTemplate(
            organization_id=organization.id,
            channel_code=channel_code,
            name=payload.name.strip(),
            renderer_type=payload.renderer_type.strip(),
            item_types=payload.item_types.strip(),
            description=payload.description.strip() if payload.description else None,
            status=payload.status,
            created_by=current_user.id,
            updated_by=current_user.id,
            data_json={},
        )
    else:
        template = existing
        template.renderer_type = payload.renderer_type.strip()
        template.item_types = payload.item_types.strip()
        template.description = payload.description.strip() if payload.description else None
        template.status = payload.status
        template.deleted_at = None
        template.updated_by = current_user.id

    db.add(template)
    db.flush()
    channel_names = _channel_name_map(db, organization.id)
    template.data_json = _build_template_json(
        template,
        channel_names.get(template.channel_code, template.channel_code),
        current_user.login_id,
        current_user.login_id,
    )
    db.add(template)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.template.create",
            target_type="template",
            target_id=template.id,
            after_json=template.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "템플릿이 저장되었습니다.", "id": str(template.id)})


@router.patch("/templates/{template_id}")
def update_admin_template(
    template_id: UUID,
    payload: TemplateUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    template = db.scalar(
        select(AdminTemplate).where(
            AdminTemplate.id == template_id,
            AdminTemplate.deleted_at.is_(None),
        )
    )
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")

    template_issues = _template_renderer_issues(
        channel_code=str(template.channel_code or ""),
        renderer_type=payload.renderer_type,
    )
    if template_issues:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=template_issues[0])

    before_json = template.data_json or {}
    template.name = payload.name.strip()
    template.renderer_type = payload.renderer_type.strip()
    template.item_types = payload.item_types.strip()
    template.description = payload.description.strip() if payload.description else None
    template.status = payload.status
    template.updated_by = current_user.id
    channel_names = _channel_name_map(db, template.organization_id)
    template.data_json = _build_template_json(
        template,
        channel_names.get(template.channel_code, template.channel_code),
        before_json.get("creator_name"),
        current_user.login_id,
    )
    db.add(template)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.template.update",
            target_type="template",
            target_id=template.id,
            before_json=before_json,
            after_json=template.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "템플릿 정보가 변경되었습니다."})


@router.delete("/templates/{template_id}")
def delete_admin_template(
    template_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    template = db.scalar(
        select(AdminTemplate).where(
            AdminTemplate.id == template_id,
            AdminTemplate.deleted_at.is_(None),
        )
    )
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="템플릿을 찾을 수 없습니다.")

    template.deleted_at = datetime.now(timezone.utc)
    template.updated_by = current_user.id
    db.add(template)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.template.delete",
            target_type="template",
            target_id=template.id,
            before_json=template.data_json,
            after_json={"deleted": True},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "템플릿이 삭제되었습니다."})


@router.get("/default-messages")
def list_default_messages(
    request: Request,
    query: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    language: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    organization = _get_default_organization(db)
    _ensure_default_messages(db, organization)
    db.commit()

    normalized_language = None
    if language:
        normalized_language = normalize_supported_language(language)
        if normalized_language is None:
            raise HTTPException(status_code=422, detail="Unsupported language.")

    statement = (
        select(AdminDefaultMessage)
        .where(
            AdminDefaultMessage.organization_id == organization.id,
            AdminDefaultMessage.deleted_at.is_(None),
        )
    )
    if normalized_language is not None:
        statement = statement.where(AdminDefaultMessage.language == normalized_language)
    messages = db.execute(
        statement.order_by(AdminDefaultMessage.category.asc(), AdminDefaultMessage.message_key.asc())
    ).scalars().all()

    if query:
        lowered = query.strip().lower()
        messages = [
            message
            for message in messages
            if lowered in message.message_key.lower()
            or lowered in message.message_name.lower()
            or lowered in message.message_text.lower()
            or lowered in (message.description or "").lower()
        ]
    if category:
        messages = [message for message in messages if message.category == category]
    if status_filter == "active":
        messages = [message for message in messages if message.status == "active"]
    elif status_filter == "inactive":
        messages = [message for message in messages if message.status == "inactive"]

    actor_names = _actor_name_map(
        db,
        {message.updated_by for message in messages if message.updated_by is not None},
    )
    items = [
        _serialize_default_message(
            message,
            actor_names.get(message.updated_by, "SYSTEM") if message.updated_by is not None else "SYSTEM",
        )
        for message in messages
    ]
    return success_response(request, {"items": [item.model_dump() for item in items], "total": len(items)})


@router.patch("/default-messages/{message_id}")
def update_default_message(
    message_id: UUID,
    payload: DefaultMessageUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    message = db.scalar(
        select(AdminDefaultMessage).where(
            AdminDefaultMessage.id == message_id,
            AdminDefaultMessage.deleted_at.is_(None),
        )
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기본 메시지를 찾을 수 없습니다.")

    before_json = message.data_json or {}
    message.message_text = payload.message_text.strip()
    message.description = payload.description.strip() if payload.description else None
    message.status = "active"
    message.scope = "global"
    message.updated_by = current_user.id
    message.data_json = _build_default_message_json(message, current_user.login_id)
    db.add(message)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.default_message.update",
            target_type="default_message",
            target_id=message.id,
            before_json=before_json,
            after_json=message.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "기본 메시지가 저장되었습니다."})


@router.post("/default-messages/{message_id}/restore")
def restore_default_message(
    message_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    message = db.scalar(
        select(AdminDefaultMessage).where(
            AdminDefaultMessage.id == message_id,
            AdminDefaultMessage.deleted_at.is_(None),
        )
    )
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기본 메시지를 찾을 수 없습니다.")

    default_item = _default_message_definition(message.message_key, message.language)
    if default_item is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="복원할 기본 문구가 없습니다.")

    before_json = message.data_json or {}
    message.message_text = default_item["message_text"].strip()
    message.description = default_item["description"]
    message.status = "active"
    message.scope = "global"
    message.updated_by = current_user.id
    message.data_json = _build_default_message_json(message, current_user.login_id)
    db.add(message)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.default_message.restore",
            target_type="default_message",
            target_id=message.id,
            before_json=before_json,
            after_json=message.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "기본 메시지가 기본값으로 복원되었습니다."})



@router.get("/license")
def get_admin_license_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_operations_view_user(db, current_user)
    organization = _get_default_organization(db)
    response = _serialize_license_status(db, organization, _current_admin_license(db, organization.id))
    return success_response(request, response.model_dump(mode="json"))


@router.post("/license/apply")
def apply_admin_license(
    payload: LicenseApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)
    organization = _get_default_organization(db)
    license_doc = _verify_license_text(payload.license_text)
    license_payload = license_doc["payload"]
    signature = license_doc["signature"]
    customer = license_payload.get("customer") if isinstance(license_payload.get("customer"), dict) else {}

    license_id = str(license_payload.get("license_id") or "").strip()
    existing = db.scalar(
        select(AdminLicense).where(
            AdminLicense.organization_id == organization.id,
            AdminLicense.license_id == license_id,
        )
    )
    current_active = db.execute(
        select(AdminLicense).where(
            AdminLicense.organization_id == organization.id,
            AdminLicense.status == "active",
        )
    ).scalars()
    for record in current_active:
        if existing is None or record.id != existing.id:
            record.status = "replaced"

    if existing is None:
        existing = AdminLicense(
            organization_id=organization.id,
            license_id=license_id,
            product=str(license_payload.get("product") or "CGA"),
            customer_name=str(customer.get("name") or ""),
            issued_at_text=license_payload.get("issued_at") if isinstance(license_payload.get("issued_at"), str) else None,
            expires_at_text=license_payload.get("expires_at") if isinstance(license_payload.get("expires_at"), str) else None,
            status="active",
            license_text=payload.license_text,
            signature_value=str(signature.get("value") or ""),
            payload_json=license_payload,
            applied_by=current_user.id,
        )
        db.add(existing)
    else:
        existing.product = str(license_payload.get("product") or "CGA")
        existing.customer_name = str(customer.get("name") or "")
        existing.issued_at_text = license_payload.get("issued_at") if isinstance(license_payload.get("issued_at"), str) else None
        existing.expires_at_text = license_payload.get("expires_at") if isinstance(license_payload.get("expires_at"), str) else None
        existing.status = "active"
        existing.license_text = payload.license_text
        existing.signature_value = str(signature.get("value") or "")
        existing.payload_json = license_payload
        existing.applied_by = current_user.id

    db.commit()
    db.refresh(existing)
    response = _serialize_license_status(db, organization, existing)
    return success_response(request, response.model_dump(mode="json"))
@router.get("/common-variables")
def list_common_variables(
    request: Request,
    query: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    _ensure_system_common_variables(db, organization)
    db.commit()

    variables = db.execute(
        select(CommonVariable)
        .where(
            CommonVariable.organization_id == organization.id,
            CommonVariable.deleted_at.is_(None),
        )
        .order_by(CommonVariable.kind.desc(), CommonVariable.name.asc())
    ).scalars().all()

    if query:
        lowered = query.strip().lower()
        variables = [
            variable
            for variable in variables
            if lowered in variable.name.lower()
            or lowered in variable.value.lower()
            or lowered in (variable.description or "").lower()
        ]
    if kind in {"system", "user"}:
        variables = [variable for variable in variables if variable.kind == kind]

    actor_names = _actor_name_map(
        db,
        {variable.updated_by for variable in variables if variable.updated_by is not None},
    )
    items = [
        _serialize_common_variable(
            variable,
            "SYSTEM" if variable.kind == "system" else actor_names.get(variable.updated_by, "SYSTEM"),
        )
        for variable in variables
    ]

    return success_response(
        request,
        {
            "items": [item.model_dump() for item in items],
            "total": len(items),
        },
    )


@router.post("/common-variables")
def create_common_variable(
    payload: CommonVariableCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    name = payload.name.strip()
    existing = db.scalar(
        select(CommonVariable).where(
            CommonVariable.organization_id == organization.id,
            CommonVariable.name == name,
        )
    )
    if existing is not None and existing.deleted_at is None and existing.kind == "system":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="시스템 변수와 같은 이름은 사용할 수 없습니다.",
        )

    if existing is None:
        variable = CommonVariable(
            organization_id=organization.id,
            kind="user",
            name=name,
            value=payload.value.strip(),
            description=payload.description.strip() if payload.description else None,
            created_by=current_user.id,
            updated_by=current_user.id,
            data_json={},
        )
    else:
        variable = existing
        variable.kind = "user"
        variable.value = payload.value.strip()
        variable.description = payload.description.strip() if payload.description else None
        variable.deleted_at = None
        variable.updated_by = current_user.id

    db.add(variable)
    db.flush()
    variable.data_json = _build_common_variable_json(variable, current_user.login_id)
    db.add(variable)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.common_variable.create",
            target_type="common_variable",
            target_id=variable.id,
            after_json=variable.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "공통 변수가 저장되었습니다.", "id": str(variable.id)})


@router.patch("/common-variables/{variable_id}")
def update_common_variable(
    variable_id: UUID,
    payload: CommonVariableUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    variable = db.scalar(
        select(CommonVariable).where(
            CommonVariable.id == variable_id,
            CommonVariable.deleted_at.is_(None),
        )
    )
    if variable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공통 변수를 찾을 수 없습니다.")
    if variable.kind == "system":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시스템 변수는 수정할 수 없습니다.")

    before_json = variable.data_json or {}
    variable.value = payload.value.strip()
    variable.description = payload.description.strip() if payload.description else None
    variable.updated_by = current_user.id
    variable.data_json = _build_common_variable_json(variable, current_user.login_id)
    db.add(variable)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.common_variable.update",
            target_type="common_variable",
            target_id=variable.id,
            before_json=before_json,
            after_json=variable.data_json,
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "공통 변수가 수정되었습니다."})


@router.post("/common-variables/import")
def import_common_variables(
    payload: CommonVariableImportRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    organization = _get_default_organization(db)
    saved_count = 0
    skipped_count = 0

    for item in payload.items:
        name = item.name.strip()
        existing = db.scalar(
            select(CommonVariable).where(
                CommonVariable.organization_id == organization.id,
                CommonVariable.name == name,
            )
        )
        if existing is not None and existing.kind == "system" and existing.deleted_at is None:
            skipped_count += 1
            continue
        if existing is None:
            variable = CommonVariable(
                organization_id=organization.id,
                kind="user",
                name=name,
                value=item.value.strip(),
                description=item.description.strip() if item.description else None,
                created_by=current_user.id,
                updated_by=current_user.id,
                data_json={"protected": True},
            )
        else:
            variable = existing
            variable.kind = "user"
            variable.value = item.value.strip()
            variable.description = item.description.strip() if item.description else None
            variable.deleted_at = None
            variable.updated_by = current_user.id
        db.add(variable)
        db.flush()
        variable.data_json = _build_common_variable_json(variable, current_user.login_id)
        db.add(variable)
        saved_count += 1

    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.common_variable.import",
            target_type="common_variable",
            target_id=None,
            after_json={"saved_count": saved_count, "skipped_count": skipped_count},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(
        request,
        {
            "message": "공통 변수 업로드가 완료되었습니다.",
            "saved_count": saved_count,
            "skipped_count": skipped_count,
        },
    )


@router.delete("/common-variables/{variable_id}")
def delete_common_variable(
    variable_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _require_admin_user(db, current_user)

    variable = db.scalar(
        select(CommonVariable).where(
            CommonVariable.id == variable_id,
            CommonVariable.deleted_at.is_(None),
        )
    )
    if variable is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="공통 변수를 찾을 수 없습니다.")
    if variable.kind == "system":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="시스템 변수는 삭제할 수 없습니다.")

    variable.deleted_at = datetime.now(timezone.utc)
    variable.updated_by = current_user.id
    db.add(variable)
    db.add(
        AuditLog(
            actor_user_id=current_user.id,
            action_type="admin.common_variable.delete",
            target_type="common_variable",
            target_id=variable.id,
            before_json=variable.data_json,
            after_json={"deleted": True},
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()
    return success_response(request, {"message": "공통 변수가 삭제되었습니다."})
