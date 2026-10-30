from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.api.routes.channels import (
    ChannelMessageRequest,
    ChannelRoomCreateRequest,
    _as_dict,
    _ensure_channel,
    _is_credential_room,
    _list_active_channel_bots,
    _serialize_room,
    _verify_channel_key,
    create_channel_room,
    create_channel_room_message,
)
from app.core.responses import success_response
from app.db.session import SessionLocal
from app.models import ChannelRoom


router = APIRouter(prefix="/am", tags=["am"])


class AmBaseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    channel_type: str = Field(default="webchat", alias="channelType")
    client_room_id: str | None = Field(default=None, alias="clientRoomId", max_length=150)
    participant_id: str | None = Field(default="visitor", alias="participantId", max_length=120)
    participant_name: str | None = Field(default="사용자", alias="participantName", max_length=120)
    seq_num: int | None = Field(default=None, alias="seqNum", ge=0)
    mode: str | None = Field(default=None, max_length=20)
    user_profile: dict[str, Any] | None = Field(default=None, alias="userProfile")
    version: str | None = Field(default=None, max_length=80)
    default_bot_id: str | None = Field(default=None, alias="defaultBotId", max_length=120)
    target_user_id: str | None = Field(default=None, alias="targetUserId", max_length=150)
    target_channel: str | None = Field(default=None, alias="targetChannel", max_length=80)
    dialog_id: str | None = Field(default=None, alias="dialogId", max_length=150)
    dialog_params: dict[str, Any] = Field(default_factory=dict, alias="dialogParams")
    system_name: str | None = Field(default=None, alias="systemName", max_length=150)
    defer_processing: bool = Field(default=False, alias="deferProcessing")

    suppress_initial_messages: bool = Field(default=False, alias="suppressInitialMessages")


class AmRoomCreateRequest(AmBaseRequest):
    pass


class AmSessionStartRequest(AmBaseRequest):
    room_id: UUID | None = Field(default=None, alias="roomId")
    session_id: str | None = Field(default=None, alias="sessionId", max_length=200)


class AmChatRequest(AmBaseRequest):
    room_id: UUID | None = Field(default=None, alias="roomId")
    session_id: str | None = Field(default=None, alias="sessionId", max_length=200)
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    source_talk_node_id: str | None = Field(default=None, alias="sourceTalkNodeId", max_length=120)

    direct_dialog_root: bool = Field(default=False, alias="directDialogRoot")


class AmDialogStartRequest(AmBaseRequest):
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    source_talk_node_id: str | None = Field(default=None, alias="sourceTalkNodeId", max_length=120)


class AmSessionEndRequest(AmBaseRequest):
    room_id: UUID | None = Field(default=None, alias="roomId")
    session_id: str | None = Field(default=None, alias="sessionId", max_length=200)


def _data(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("data")
    return value if isinstance(value, dict) else {}


def _is_manual_request(payload: AmBaseRequest) -> bool:
    return any(
        value is not None
        for value in (
            payload.seq_num,
            payload.mode,
            payload.user_profile,
            payload.version,
            payload.default_bot_id,
            payload.dialog_id,
            payload.target_user_id,
        )
    )


def _profile_value(payload: AmBaseRequest, key: str, fallback: str) -> str:
    profile = payload.user_profile or {}
    value = profile.get(key)
    return str(value).strip() if value is not None and str(value).strip() else fallback


def _request_participant_id(payload: AmBaseRequest) -> str:
    if payload.participant_id and payload.participant_id != "visitor":
        return payload.participant_id
    return _profile_value(payload, "userId", payload.target_user_id or "visitor")


def _request_participant_name(payload: AmBaseRequest) -> str:
    if payload.participant_name and payload.participant_name != "사용자":
        return payload.participant_name
    return _profile_value(payload, "userName", "사용자")


def _manual_session_id(payload: AmBaseRequest, data: dict[str, Any]) -> str:
    if payload.session_id:
        return payload.session_id
    return str(data.get("sessionId") or data.get("roomId") or "")


def _template_message(message: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(message.get("payload"))
    options = payload.get("options") or message.get("options") or []
    message_type = str(message.get("messageType") or message.get("type") or "text")
    return {
        "templateId": str(payload.get("templateId") or message_type),
        "message": str(message.get("text") or payload.get("message") or ""),
        "templateFields": payload.get("templateFields") or payload.get("fields") or [],
        "intent": payload.get("intent"),
        "userResponseType": payload.get("userResponseType") or "NONE",
        "properties": payload.get("properties") or {},
        "buttonFieldValue": payload.get("buttonFieldValue") or options,
        "optionButtonFieldValue": payload.get("optionButtonFieldValue") or options,
        "ynoptionButtonFieldValue": payload.get("ynoptionButtonFieldValue") or [],
        "imageDescFieldValue": payload.get("imageDescFieldValue") or payload.get("imageDescription"),
        "imageUrlFieldValue": payload.get("imageUrlFieldValue") or payload.get("imageUrl"),
        "tableValue": payload.get("tableValue") or payload.get("table"),
    }


def _manual_response(
    bot_id: str,
    payload: AmBaseRequest,
    data: dict[str, Any],
    response_code: str = "C20000",
    description: str | None = None,
) -> dict[str, Any]:
    messages = data.get("botMessages") or data.get("initialMessages") or data.get("messages") or []
    normalized_messages = [item for item in messages if isinstance(item, dict)]
    active_version = _as_dict(data.get("activeVersion"))
    return {
        "botId": bot_id,
        "sessionId": _manual_session_id(payload, data),
        "seqNum": payload.seq_num if payload.seq_num is not None else 0,
        "responseCode": response_code,
        "version": active_version.get("versionNo") or active_version.get("name") or payload.version,
        "templateMessages": [_template_message(item) for item in normalized_messages],
        "description": description or "",
        "reasonCode": data.get("reasonCode") or "",
        "reasonDesc": data.get("reasonDesc") or "",
        "analysisResponses": data.get("analysisResponses") or [],
        "masterBotResponse": data.get("masterBotResponse") or {},
        "queued": bool(data.get("queued")),
        "queueEvent": _as_dict(data.get("queueEvent")),
    }


def _validate_manual_mode(payload: AmBaseRequest) -> None:
    if payload.mode and payload.mode.upper() not in {"DEV", "MANUAL", "LAST", "SETUP"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="지원하지 않는 세션 모드입니다.")


def _target_channel(payload: AmDialogStartRequest) -> str:
    target = (payload.target_channel or "").strip().upper()
    channel = {"CM_CHAT": "webchat", "WEBCHAT": "webchat", "KAKAO": "kakao"}.get(target)
    return channel or payload.channel_type


def _resolve_bot_id(bot_id: str, channel_type: str) -> str:
    with SessionLocal() as db:
        for bot, _version, _group in _list_active_channel_bots(db, channel_type, include_runtime_blocked=True):
            if str(bot.id) == bot_id:
                return str(bot.id)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="운영 채널 봇을 찾을 수 없습니다.")


def _room_create_payload(bot_id: str, payload: AmBaseRequest) -> ChannelRoomCreateRequest:
    channel = _ensure_channel(payload.channel_type)
    return ChannelRoomCreateRequest(
        bot_id=_resolve_bot_id(bot_id, channel),
        client_room_id=payload.client_room_id,
        participant_id=_request_participant_id(payload),
        participant_name=_request_participant_name(payload),
        start_immediately=not payload.suppress_initial_messages,
    )


def _room_create_request(payload: AmBaseRequest, session_id_as_client_room: bool = False) -> AmRoomCreateRequest:
    client_room_id = payload.client_room_id
    if session_id_as_client_room and not client_room_id and payload.session_id:
        client_room_id = payload.session_id
    return AmRoomCreateRequest(
        channelType=payload.channel_type,
        clientRoomId=client_room_id,
        participantId=_request_participant_id(payload),
        participantName=_request_participant_name(payload),
        seqNum=payload.seq_num,
        mode=payload.mode,
        userProfile=payload.user_profile,
        version=payload.version,
        defaultBotId=payload.default_bot_id,
        suppressInitialMessages=payload.suppress_initial_messages,
    )


def _resolve_room(channel_type: str, room_id: UUID | None, session_id: str | None, client_room_id: str | None) -> ChannelRoom:
    channel = _ensure_channel(channel_type)
    target_room_id = room_id
    lookup_client_room_id = client_room_id
    if target_room_id is None and session_id:
        try:
            target_room_id = UUID(session_id)
        except ValueError:
            lookup_client_room_id = session_id
    with SessionLocal() as db:
        conditions: list[Any] = [ChannelRoom.channel_type == channel, ChannelRoom.status == "open", ChannelRoom.deleted_at.is_(None)]
        if target_room_id is not None:
            conditions.append(ChannelRoom.id == target_room_id)
        elif lookup_client_room_id:
            conditions.append(ChannelRoom.client_room_id == lookup_client_room_id)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="roomId, sessionId 또는 clientRoomId가 필요합니다.")
        room = db.scalar(select(ChannelRoom).where(*conditions).order_by(ChannelRoom.updated_at.desc()))
        if room is None or _is_credential_room(room):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="열린 대화 세션을 찾을 수 없습니다.")
        return room


@router.post("/{bot_id}/rooms")
def create_am_room(
    bot_id: str,
    payload: AmRoomCreateRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, Any]:
    channel = _ensure_channel(payload.channel_type)
    _verify_channel_key(x_aidot_webchat_key)
    response = create_channel_room(channel, _room_create_payload(bot_id, payload), request, x_aidot_webchat_key)
    result = _data(response)
    room = _as_dict(result.get("room"))
    return success_response(
        request,
        {
            **result,
            "sessionId": room.get("id"),
            "roomId": room.get("id"),
            "channelType": channel,
        },
    )


@router.post("/{bot_id}/session/start")
def start_am_session(
    bot_id: str,
    payload: AmSessionStartRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, Any]:
    channel = _ensure_channel(payload.channel_type)
    _verify_channel_key(x_aidot_webchat_key)
    manual_request = _is_manual_request(payload)
    if manual_request:
        _validate_manual_mode(payload)
    if payload.room_id or payload.session_id:
        try:
            room = _resolve_room(channel, payload.room_id, payload.session_id, payload.client_room_id)
        except HTTPException as error:
            if not manual_request or error.status_code != status.HTTP_404_NOT_FOUND:
                raise
            room = None
        if room is None:
            room_response = create_am_room(
                bot_id,
                _room_create_request(payload, session_id_as_client_room=True),
                request,
                x_aidot_webchat_key,
            )
            return _manual_response(bot_id, payload, _data(room_response))
        with SessionLocal() as db:
            db_room = db.get(ChannelRoom, room.id)
            if db_room is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대화 세션을 찾을 수 없습니다.")
            result = {
                    "room": _serialize_room(db, db_room),
                    "sessionId": str(db_room.id),
                    "roomId": str(db_room.id),
                    "channelType": channel,
                    "initialMessages": [],
                }
            if manual_request:
                return _manual_response(bot_id, payload, result)
            return success_response(
                request,
                result,
            )
    room_response = create_am_room(
        bot_id,
        _room_create_request(payload, session_id_as_client_room=manual_request),
        request,
        x_aidot_webchat_key,
    )
    if manual_request:
        return _manual_response(bot_id, payload, _data(room_response))
    return room_response


@router.post("/{bot_id}/chat")
def send_am_chat(
    bot_id: str,
    payload: AmChatRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, Any]:
    channel = _ensure_channel(payload.channel_type)
    _verify_channel_key(x_aidot_webchat_key)
    message = payload.text or payload.message
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="text 또는 message가 필요합니다.")
    manual_request = _is_manual_request(payload) or payload.text is not None
    room = _resolve_room(channel, payload.room_id, payload.session_id, payload.client_room_id)
    response = create_channel_room_message(
        channel,
        room.id,
        ChannelMessageRequest(
            message=message,
            participant_id=_request_participant_id(payload) or room.participant_id,
            source_talk_node_id=payload.source_talk_node_id,
            target_dialog_id=payload.dialog_id,
            dialog_params=payload.dialog_params,
            system_name=payload.system_name,
            defer_processing=payload.defer_processing,
            direct_dialog_root=payload.direct_dialog_root,
        ),
        request,
        x_aidot_webchat_key,
    )
    result = _data(response)
    if manual_request:
        response_code = "C20000" if result.get("botMessages") or result.get("queued") else "C40009"
        description = "Queue에 적재되었습니다." if result.get("queued") else None
        return _manual_response(bot_id, payload, result, response_code=response_code, description=description)
    return success_response(
        request,
        {
            **result,
            "sessionId": str(room.id),
            "roomId": str(room.id),
            "channelType": channel,
        },
    )


@router.post("/{bot_id}/dialog/start")
def start_am_dialog(
    bot_id: str,
    payload: AmDialogStartRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, Any]:
    channel = _ensure_channel(_target_channel(payload))
    _verify_channel_key(x_aidot_webchat_key)
    if payload.dialog_id:
        if not payload.target_user_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="targetUserId가 필요합니다.")
        try:
            _resolve_room(channel, None, payload.target_user_id, payload.target_user_id)
            existing_room = True
        except HTTPException as error:
            if error.status_code != status.HTTP_404_NOT_FOUND:
                raise
            existing_room = False
        profile = payload.user_profile or {
            "userId": payload.target_user_id,
            "userName": payload.target_user_id,
            "channel": payload.target_channel or channel,
        }
        start_am_session(
            bot_id,
            AmSessionStartRequest(
                channelType=channel,
                clientRoomId=payload.target_user_id,
                participantId=payload.target_user_id,
                participantName=_profile_value(payload, "userName", payload.target_user_id),
                sessionId=payload.target_user_id,
                seqNum=payload.seq_num,
                mode=payload.mode or "MANUAL",
                userProfile=profile,
                version=payload.version,
                defaultBotId=payload.default_bot_id,
                targetUserId=payload.target_user_id,
                targetChannel=payload.target_channel,
                dialogId=payload.dialog_id,
                dialogParams=payload.dialog_params,
                systemName=payload.system_name,
                suppressInitialMessages=not existing_room,
            ),
            request,
            x_aidot_webchat_key,
        )
        room = _resolve_room(channel, None, None, payload.target_user_id)
        return send_am_chat(
            bot_id,
            AmChatRequest(
                channelType=channel,
                roomId=str(room.id),
                sessionId=payload.target_user_id,
                participantId=payload.target_user_id,
                text="__external_dialog_start__",
                seqNum=payload.seq_num,
                userProfile=profile,
                dialogId=payload.dialog_id,
                dialogParams=payload.dialog_params,
                systemName=payload.system_name,
                deferProcessing=True,
                directDialogRoot=not existing_room,
            ),
            request,
            x_aidot_webchat_key,
        )
    if not payload.message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="dialogId 또는 message가 필요합니다.")
    session_response = start_am_session(
        bot_id,
        AmSessionStartRequest(**payload.model_dump(by_alias=True)),
        request,
        x_aidot_webchat_key,
    )
    session_data = _data(session_response)
    room_id = session_data.get("roomId") or session_data.get("sessionId")
    if not room_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="대화 세션을 시작하지 못했습니다.")
    chat_response = send_am_chat(
        bot_id,
        AmChatRequest(**{**payload.model_dump(by_alias=True), "roomId": room_id}),
        request,
        x_aidot_webchat_key,
    )
    result = _data(chat_response)
    return success_response(request, {**result, "started": True})


@router.post("/{bot_id}/session/end")
def end_am_session(
    bot_id: str,
    payload: AmSessionEndRequest,
    request: Request,
    x_aidot_webchat_key: str | None = Header(default=None),
) -> dict[str, Any]:
    channel = _ensure_channel(payload.channel_type)
    _verify_channel_key(x_aidot_webchat_key)
    manual_request = _is_manual_request(payload)
    _resolve_bot_id(bot_id, channel)
    room = _resolve_room(channel, payload.room_id, payload.session_id, payload.client_room_id)
    with SessionLocal() as db:
        db_room = db.get(ChannelRoom, room.id)
        if db_room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대화 세션을 찾을 수 없습니다.")
        metadata = _as_dict(db_room.metadata_json)
        metadata["sessionEndReason"] = "external_session_end"
        metadata["endedAt"] = datetime.now(timezone.utc).isoformat()
        db_room.metadata_json = metadata
        db_room.status = "closed"
        db_room.updated_at = datetime.now(timezone.utc)
        flag_modified(db_room, "metadata_json")
        db.add(db_room)
        db.commit()
        db.refresh(db_room)
        result = {
                "sessionId": str(db_room.id),
                "roomId": str(db_room.id),
                "channelType": channel,
                "ended": True,
                "room": _serialize_room(db, db_room),
            }
        if manual_request:
            return _manual_response(bot_id, payload, result, response_code="C20003", description="세션이 종료되었습니다.")
        return success_response(
            request,
            result,
        )
