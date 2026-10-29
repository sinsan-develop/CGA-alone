from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.responses import success_response
from app.models import AuditLog, Bot, BotHub, BotHubMember, BotVersion, ChannelMessage, ChannelQueueEvent, User
from app.schemas.hub import HubMembersUpdateRequest, HubSettingsUpdateRequest


router = APIRouter(prefix="/hubs", tags=["hubs"])


def _require_group_user(current_user: User) -> UUID:
    if current_user.group_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="그룹이 지정된 사용자만 봇 허브에 접근할 수 있습니다.")
    return current_user.group_id


def _hub_call_rules(bot: Bot) -> list[dict[str, object]]:
    data = bot.data_json if isinstance(bot.data_json, dict) else {}
    raw_rules = data.get("hub_call_rules")
    if not isinstance(raw_rules, list):
        return []
    rules: list[dict[str, object]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            continue
        expression = str(raw_rule.get("expression") or "").strip()
        if not expression:
            continue
        name = str(raw_rule.get("name") or expression).strip() or expression
        rules.append(
            {
                "id": str(raw_rule.get("id") or "").strip() or str(uuid4()),
                "name": name,
                "expression": expression,
                "enabled": raw_rule.get("enabled") is not False,
            }
        )
    return rules


def _normalize_hub_call_rules(raw_rules: list[dict[str, object]]) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_rule in raw_rules:
        expression = str(raw_rule.get("expression") or "").strip()
        normalized_expression = expression.casefold()
        if not expression:
            continue
        if normalized_expression in seen:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="같은 봇 허브 호출 단어를 두 번 등록할 수 없습니다.")
        seen.add(normalized_expression)
        name = str(raw_rule.get("name") or expression).strip() or expression
        rule_id = str(raw_rule.get("id") or "").strip() or str(uuid4())
        rules.append({"id": rule_id, "name": name, "expression": expression, "enabled": raw_rule.get("enabled") is not False})
    return rules


def _get_hub_bot(db: Session, current_user: User, hub_id: UUID) -> tuple[Bot, BotHub]:
    group_id = _require_group_user(current_user)
    bot = db.scalar(
        select(Bot).where(
            Bot.id == hub_id,
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == group_id,
            Bot.created_by == current_user.id,
            Bot.deleted_at.is_(None),
        )
    )
    if bot is None or (bot.data_json or {}).get("bot_kind") != "hub":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="봇 허브를 찾을 수 없습니다.")
    hub = db.get(BotHub, hub_id)
    if hub is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="봇 허브 설정이 초기화되지 않았습니다.")
    return bot, hub


def _serialize_hub(db: Session, bot: Bot, hub: BotHub, *, include_members: bool = True) -> dict[str, object]:
    active_version = db.get(BotVersion, bot.active_version_id) if bot.active_version_id else None
    result: dict[str, object] = {
        "id": str(bot.id),
        "name": bot.name,
        "description": bot.description,
        "status": bot.status,
        "active_version_id": str(bot.active_version_id) if bot.active_version_id else None,
        "active_version_name": active_version.name if active_version else None,
        "profile_key": (bot.data_json or {}).get("profile_key") or "accent",
        "profile_image_url": (bot.data_json or {}).get("profile_image_url"),
        "language": (bot.data_json or {}).get("language") or "ko",
        "introduction": (bot.data_json or {}).get("introduction"),
        "hub_call_rules": _hub_call_rules(bot),
        "call_method": hub.call_method,
        "button_match_mode": hub.button_match_mode,
        "greeting_message": hub.greeting_message,
        "intent_cutoff_score": hub.intent_cutoff_score,
        "similar_intent_score": hub.similar_intent_score,
        "max_intent_candidates": hub.max_intent_candidates,
        "show_members_in_greeting": hub.show_members_in_greeting,
        "unrecognized_message": hub.unrecognized_message,
        "multiple_candidates_message": hub.multiple_candidates_message,
        "runtime_error_message": hub.runtime_error_message,
        "conversation_in_progress_message": hub.conversation_in_progress_message,
        "timeout_seconds": hub.timeout_seconds,
        "apply_timeout_to_push": hub.apply_timeout_to_push,
        "timeout_message": hub.timeout_message,
        "no_bot_label": hub.no_bot_label,
        "no_bot_message": hub.no_bot_message,
        "created_at": bot.created_at.isoformat() if bot.created_at else None,
        "updated_at": bot.updated_at.isoformat() if bot.updated_at else None,
    }
    if not include_members:
        return result

    members = list(
        db.scalars(
            select(BotHubMember)
            .where(BotHubMember.hub_id == bot.id)
            .order_by(BotHubMember.sort_order, BotHubMember.created_at)
        ).all()
    )
    member_bot_ids = [member.bot_id for member in members]
    member_bots = {
        item.id: item
        for item in db.scalars(
            select(Bot).where(Bot.id.in_(member_bot_ids), Bot.deleted_at.is_(None))
        ).all()
    } if member_bot_ids else {}
    active_versions = {
        version.id: version
        for version in db.scalars(
            select(BotVersion).where(BotVersion.id.in_([item.active_version_id for item in member_bots.values() if item.active_version_id is not None]), BotVersion.deleted_at.is_(None))
        ).all()
    } if member_bots else {}
    result["members"] = [
        {
            "id": str(member.id),
            "bot_id": str(member.bot_id),
            "name": member_bots[member.bot_id].name if member.bot_id in member_bots else None,
            "display_name": member.display_name,
            "sort_order": member.sort_order,
            "use_as_small_talk": member.use_as_small_talk,
            "has_operating_version": bool(member.bot_id in member_bots and member_bots[member.bot_id].active_version_id in active_versions),
            "active_version_id": str(member_bots[member.bot_id].active_version_id) if member.bot_id in member_bots and member_bots[member.bot_id].active_version_id else None,
        }
        for member in members
    ]
    return result


def _write_audit(db: Session, request: Request, current_user: User, action_type: str, hub_id: UUID, before: dict[str, object] | None, after: dict[str, object] | None) -> None:
    db.add(AuditLog(
        actor_user_id=current_user.id,
        action_type=action_type,
        target_type="bot_hub",
        target_id=hub_id,
        before_json=before,
        after_json=after,
        ip_address=request.client.host if request.client else None,
    ))


@router.get("")
def list_hubs(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    group_id = _require_group_user(current_user)
    rows = db.execute(
        select(Bot, BotHub)
        .join(BotHub, BotHub.bot_id == Bot.id)
        .where(
            Bot.organization_id == current_user.organization_id,
            Bot.group_id == group_id,
            Bot.created_by == current_user.id,
            Bot.deleted_at.is_(None),
        )
        .order_by(Bot.updated_at.desc())
    ).all()
    return success_response(request, [_serialize_hub(db, bot, hub, include_members=True) for bot, hub in rows])


@router.get("/{hub_id}")
def get_hub(
    hub_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, hub = _get_hub_bot(db, current_user, hub_id)
    return success_response(request, _serialize_hub(db, bot, hub))




def _hub_retraining_result_kind(event: ChannelQueueEvent) -> str:
    hub_data = event.parameter_json.get("hub") if isinstance(event.parameter_json, dict) else {}
    selected_bot_id = hub_data.get("selectedBotId") if isinstance(hub_data, dict) else None
    if event.error_message or str(event.status).lower() in {"failed", "error"}:
        return "error"
    if event.intent_name:
        return "intent_classified"
    if selected_bot_id:
        return "bot_unclassified"
    return "hub_unclassified"


@router.get("/{hub_id}/retraining-candidates")
def list_hub_retraining_candidates(
    hub_id: UUID,
    request: Request,
    member_bot_id: UUID | None = None,
    channel_type: str | None = None,
    result_kind: str | None = Query(default=None, pattern="^(intent_classified|bot_unclassified|hub_unclassified|error)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, _hub = _get_hub_bot(db, current_user, hub_id)
    member_rows = list(
        db.scalars(
            select(BotHubMember)
            .where(BotHubMember.hub_id == hub_id)
            .order_by(BotHubMember.sort_order, BotHubMember.created_at)
        ).all()
    )
    member_ids = {member.bot_id for member in member_rows}
    member_names = {
        item.id: item.name
        for item in db.scalars(
            select(Bot).where(Bot.id.in_(member_ids), Bot.deleted_at.is_(None))
        ).all()
    } if member_ids else {}

    events = list(
        db.scalars(
            select(ChannelQueueEvent)
            .where(
                ChannelQueueEvent.deleted_at.is_(None),
                ChannelQueueEvent.parameter_json["hub"]["hubBotId"].astext == str(hub_id),
            )
            .order_by(ChannelQueueEvent.created_at.desc())
        ).all()
    )
    request_message_ids = [event.request_message_id for event in events if event.request_message_id]
    request_messages = {
        message.id: message
        for message in db.scalars(
            select(ChannelMessage).where(
                ChannelMessage.id.in_(request_message_ids),
                ChannelMessage.deleted_at.is_(None),
            )
        ).all()
    } if request_message_ids else {}

    items: list[dict[str, object]] = []
    for event in events:
        parameter_json = event.parameter_json if isinstance(event.parameter_json, dict) else {}
        hub_data = parameter_json.get("hub") if isinstance(parameter_json.get("hub"), dict) else {}
        selected_bot_id_raw = hub_data.get("selectedBotId")
        try:
            selected_bot_id = UUID(str(selected_bot_id_raw)) if selected_bot_id_raw else None
        except (TypeError, ValueError):
            selected_bot_id = None
        if member_bot_id is not None and selected_bot_id != member_bot_id:
            continue
        if channel_type and event.channel_type != channel_type:
            continue
        item_result_kind = _hub_retraining_result_kind(event)
        if result_kind and item_result_kind != result_kind:
            continue
        request_message = request_messages.get(event.request_message_id) if event.request_message_id else None
        member_name = None
        if selected_bot_id:
            member_name = hub_data.get("selectedBotName") or member_names.get(selected_bot_id)
        items.append(
            {
                "queue_event_id": str(event.id),
                "member_bot_id": str(selected_bot_id) if selected_bot_id else None,
                "member_bot_name": member_name,
                "user_utterance": request_message.text if request_message else "",
                "intent_name": event.intent_name,
                "channel_type": event.channel_type,
                "status": event.status,
                "result_kind": item_result_kind,
                "error_message": event.error_message,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
        )

    total = len(items)
    offset = (page - 1) * page_size
    return success_response(
        request,
        {
            "hub_id": str(bot.id),
            "items": items[offset:offset + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "members": [
                {
                    "bot_id": str(member.bot_id),
                    "display_name": member.display_name or member_names.get(member.bot_id),
                }
                for member in member_rows
            ],
        },
    )
def _has_successful_nlu_training(version: BotVersion) -> bool:
    snapshot = version.nlu_training_json if isinstance(version.nlu_training_json, dict) else {}
    if str(snapshot.get("status") or "").lower() == "success":
        return True
    version_json = version.version_json if isinstance(version.version_json, dict) else {}
    system_config = version_json.get("system_config") if isinstance(version_json.get("system_config"), dict) else {}
    training = system_config.get("nlu_training") if isinstance(system_config.get("nlu_training"), dict) else {}
    return str(training.get("status") or "").lower() == "success"


def _ensure_natural_hub_ready(db: Session, member_ids: list[UUID]) -> None:
    if len(member_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="자연어형 봇 허브는 학습에 성공한 운영 버전의 하위 봇을 두 개 이상 구성해야 합니다.",
        )
    versions = db.scalars(
        select(BotVersion)
        .join(Bot, Bot.active_version_id == BotVersion.id)
        .where(
            Bot.id.in_(member_ids),
            Bot.deleted_at.is_(None),
            BotVersion.deleted_at.is_(None),
            BotVersion.status == "active",
        )
    ).all()
    trained_bot_ids = {
        version.bot_id
        for version in versions
        if _has_successful_nlu_training(version)
    }
    if len(trained_bot_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="자연어형 봇 허브는 학습에 성공한 운영 버전의 하위 봇을 두 개 이상 구성해야 합니다.",
        )


@router.put("/{hub_id}/members")
def replace_hub_members(
    hub_id: UUID,
    payload: HubMembersUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, hub = _get_hub_bot(db, current_user, hub_id)
    member_ids = [item.bot_id for item in payload.members]
    if len(member_ids) != len(set(member_ids)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="동일한 봇을 두 번 추가할 수 없습니다.")
    if hub_id in member_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="봇 허브 자신을 하위 봇으로 추가할 수 없습니다.")
    candidates = {
        candidate.id: candidate
        for candidate in db.scalars(
            select(Bot).where(
                Bot.id.in_(member_ids),
                Bot.organization_id == current_user.organization_id,
                Bot.group_id == current_user.group_id,
                Bot.deleted_at.is_(None),
            )
        ).all()
    } if member_ids else {}
    invalid_ids = [str(member_id) for member_id in member_ids if member_id not in candidates or (candidates[member_id].data_json or {}).get("bot_kind") == "hub"]
    if invalid_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="같은 그룹의 일반 봇만 허브에 추가할 수 있습니다.")
    operating_member_ids = {
        version.bot_id
        for version in db.scalars(
            select(BotVersion)
            .join(Bot, Bot.active_version_id == BotVersion.id)
            .where(
                Bot.id.in_(member_ids),
                BotVersion.deleted_at.is_(None),
                BotVersion.status == "active",
            )
        ).all()
    } if member_ids else set()
    missing_operating_version_ids = [str(member_id) for member_id in member_ids if member_id not in operating_member_ids]
    if missing_operating_version_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="운영 버전이 있는 일반 봇만 허브에 추가할 수 있습니다.",
        )
    before = _serialize_hub(db, bot, hub)
    db.execute(delete(BotHubMember).where(BotHubMember.hub_id == hub_id))
    for sort_order, member in enumerate(payload.members):
        db.add(BotHubMember(
            hub_id=hub_id,
            bot_id=member.bot_id,
            display_name=member.display_name.strip() if member.display_name else None,
            sort_order=sort_order,
            use_as_small_talk=member.use_as_small_talk,
        ))
    hub.updated_at = datetime.now(timezone.utc)
    db.flush()
    after = _serialize_hub(db, bot, hub)
    _write_audit(db, request, current_user, "bot_hub.members.replace", hub_id, before, after)
    db.commit()
    return success_response(request, after)



@router.delete("/{hub_id}")
def delete_hub(
    hub_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, hub = _get_hub_bot(db, current_user, hub_id)
    active_version = db.scalar(
        select(BotVersion).where(
            BotVersion.id == bot.active_version_id,
            BotVersion.deleted_at.is_(None),
        )
    ) if bot.active_version_id else None
    if active_version is not None and active_version.status == "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="\uc6b4\uc601 \ubc84\uc804\uc774 \uc788\ub294 \ubd07 \ud5c8\ube0c\ub294 \uc0ad\uc81c\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4. \uba3c\uc800 \uc6b4\uc601\uc744 \ud574\uc81c\ud574\uc8fc\uc138\uc694.",
        )

    before = _serialize_hub(db, bot, hub)
    deleted_at = datetime.now(timezone.utc)
    bot.deleted_at = deleted_at
    bot.active_version_id = None
    versions = db.scalars(
        select(BotVersion).where(
            BotVersion.bot_id == bot.id,
            BotVersion.deleted_at.is_(None),
        )
    ).all()
    for version in versions:
        version.deleted_at = deleted_at
    db.execute(delete(BotHubMember).where(BotHubMember.hub_id == hub_id))
    _write_audit(
        db,
        request,
        current_user,
        "bot_hub.delete",
        hub_id,
        before,
        {"id": str(hub_id), "deleted_at": deleted_at.isoformat()},
    )
    db.commit()
    return success_response(request, {"message": "\ubd07 \ud5c8\ube0c\uac00 \uc0ad\uc81c\ub418\uc5c8\uc2b5\ub2c8\ub2e4."})


@router.patch("/{hub_id}/settings")
def update_hub_settings(
    hub_id: UUID,
    payload: HubSettingsUpdateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    bot, hub = _get_hub_bot(db, current_user, hub_id)
    next_settings = payload.model_dump(exclude_unset=True)
    next_call_method = next_settings.get("call_method", hub.call_method)
    hub_call_rules_input = next_settings.pop("hub_call_rules", None)
    before = _serialize_hub(db, bot, hub)
    if hub_call_rules_input is not None:
        normalized_rules = _normalize_hub_call_rules(hub_call_rules_input)
        requested_expressions = {str(item["expression"]).casefold() for item in normalized_rules}
        if requested_expressions:
            other_hub_bots = db.scalars(
                select(Bot).join(BotHub, BotHub.bot_id == Bot.id).where(
                    Bot.organization_id == current_user.organization_id,
                    Bot.group_id == current_user.group_id,
                    Bot.id != bot.id,
                    Bot.deleted_at.is_(None),
                )
            ).all()
            duplicate_expression = next(
                (
                    rule["expression"]
                    for other_bot in other_hub_bots
                    for rule in _hub_call_rules(other_bot)
                    if rule["enabled"] and str(rule["expression"]).casefold() in requested_expressions
                ),
                None,
            )
            if duplicate_expression is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"다른 봇 허브에 이미 등록된 호출 단어입니다: {duplicate_expression}",
                )
        bot_data = dict(bot.data_json or {})
        bot_data["hub_call_rules"] = normalized_rules
        bot.data_json = bot_data
        db.add(bot)
    for field_name, value in next_settings.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(hub, field_name, value)
    hub.updated_at = datetime.now(timezone.utc)
    db.flush()
    after = _serialize_hub(db, bot, hub)
    _write_audit(db, request, current_user, "bot_hub.settings.update", hub_id, before, after)
    db.commit()
    return success_response(request, after)
