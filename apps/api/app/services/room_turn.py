"""방 단위 차례의 DB 원자성·세대 fence.

actor_id는 호출자가 서버 인증/참가자 바인딩으로 확인한 주체여야 한다.
이 helper는 인증을 대신하거나 body participantId를 신뢰하지 않는다.
각 함수는 caller transaction에서 flush만 한다. 선점 transaction은 즉시
commit/종료하고 외부 API를 호출한다. 답변 저장은 lock_current_room_turn으로
재확인한 짧은 transaction 안에서 수행한다. timeout/복구와 runtime 소비는 별도다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timedelta
import math
from uuid import UUID,uuid4
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from app.models import ChannelRoom,Bot,BotVersion

_TURN_KEY="__roomTurn"
_TERMINAL_REASONS=frozenset({"dialog_ended","session_ended","error","cancelled","expired","replaced","room_closed"})


@dataclass(frozen=True)
class RoomTurnDecision:
    allowed: bool
    generation: str | None
    reason: str


def _actor_valid(actor_id) -> bool:
    return isinstance(actor_id,str) and bool(actor_id.strip())


def _locked_room(db: Session, room_id: UUID) -> ChannelRoom | None:
    return db.scalar(select(ChannelRoom).where(ChannelRoom.id==room_id).with_for_update().execution_options(populate_existing=True))


def _open(room: ChannelRoom | None) -> bool:
    return room is not None and room.deleted_at is None and room.status=="open"


def _valid_turn(turn) -> bool:
    return isinstance(turn,dict) and _actor_valid(turn.get("actor_id")) and isinstance(turn.get("generation"),str) and bool(turn["generation"]) and turn.get("phase") in {"running","waiting_input"} and isinstance(turn.get("bot_id"),str) and isinstance(turn.get("bot_version_id"),str)


def _matches(turn,actor_id,generation) -> bool:
    return _valid_turn(turn) and turn["actor_id"]==actor_id and isinstance(generation,str) and turn["generation"]==generation


def _save(db: Session,room: ChannelRoom,metadata: dict) -> None:
    room.metadata_json=metadata
    flag_modified(room,"metadata_json")
    db.flush()


def _record(value):
    return value if isinstance(value,dict) else {}


def _waiting_expiry(db: Session,room: ChannelRoom) -> str | None:
    bot=db.get(Bot,room.bot_id)
    version=db.get(BotVersion,room.bot_version_id)
    by_version=_record(_record(bot.data_json if bot else None).get("settings_by_version"))
    settings=_record(by_version.get(str(room.bot_version_id))) or _record(by_version.get(version.name if version else ""))
    timeout=_record(_record(settings.get("conversationDefaults")).get("timeout"))
    if timeout.get("enabled") is False:return None
    now=db.scalar(select(func.clock_timestamp()))
    try:
        seconds=float(timeout.get("seconds",120))
        if not math.isfinite(seconds) or seconds<1:raise ValueError
        return (now+timedelta(seconds=seconds)).isoformat()
    except (ValueError,TypeError,OverflowError):
        return (now+timedelta(seconds=120)).isoformat()


def _waiting_expired(db: Session,turn: dict) -> bool:
    expires=turn.get("waiting_expires_at")
    if expires is None:return False
    deadline=datetime.fromisoformat(expires)
    if deadline.tzinfo is None:raise ValueError("대기 만료 시각은 timezone이 필요합니다.")
    return deadline<=db.scalar(select(func.clock_timestamp()))


def claim_room_turn(db: Session,room: ChannelRoom,actor_id: str,addressed: bool) -> RoomTurnDecision:
    """같은 방의 actor/실행을 직렬화한다. commit은 짧은 caller transaction의 책임이다."""
    if addressed is not True:return RoomTurnDecision(False,None,"not_addressed")
    if not _actor_valid(actor_id):return RoomTurnDecision(False,None,"unbound_actor")
    expected_bot,expected_version=room.bot_id,room.bot_version_id
    current=_locked_room(db,room.id)
    if not _open(current):return RoomTurnDecision(False,None,"room_closed")
    if current.bot_id!=expected_bot:return RoomTurnDecision(False,None,"room_changed")
    if current.bot_version_id!=expected_version:return RoomTurnDecision(False,None,"version_changed")
    if not isinstance(current.metadata_json,dict):return RoomTurnDecision(False,None,"invalid_runtime")
    metadata=deepcopy(current.metadata_json)
    reason="acquired"
    if _TURN_KEY in metadata:
        active=metadata[_TURN_KEY]
        if not _valid_turn(active):return RoomTurnDecision(False,None,"invalid_turn")
        if active["bot_id"]!=str(current.bot_id):return RoomTurnDecision(False,None,"invalid_turn")
        if active["bot_version_id"]!=str(current.bot_version_id):
            # 기존 운영 버전 전환으로 무효화된 이전 실행은 새 세대로 fence한다.
            reason="version_changed"
        else:
            try:
                expired=active["phase"]=="waiting_input" and _waiting_expired(db,active)
            except (ValueError,TypeError):
                return RoomTurnDecision(False,None,"invalid_turn")
            if expired:reason="expired"
            elif active["actor_id"]!=actor_id:return RoomTurnDecision(False,None,"another_actor")
            elif active["phase"]=="running":return RoomTurnDecision(False,None,"processing")
            else:reason="continued"
    generation=str(uuid4())
    metadata[_TURN_KEY]={"actor_id":actor_id,"generation":generation,"phase":"running","bot_id":str(current.bot_id),"bot_version_id":str(current.bot_version_id),"waiting_expires_at":None}
    _save(db,current,metadata)
    return RoomTurnDecision(True,generation,reason)


def lock_current_room_turn(db: Session,room_id: UUID,actor_id: str,generation: str) -> ChannelRoom | None:
    """답변/변수 저장 직전에 actor·세대·버전을 확인하고 transaction 동안 잠근다."""
    if not _actor_valid(actor_id) or not isinstance(generation,str) or not generation:return None
    room=_locked_room(db,room_id)
    if not _open(room) or not isinstance(room.metadata_json,dict):return None
    turn=room.metadata_json.get(_TURN_KEY)
    if not _matches(turn,actor_id,generation) or turn["phase"]!="running":return None
    if turn["bot_id"]!=str(room.bot_id) or turn["bot_version_id"]!=str(room.bot_version_id):return None
    return room


def hold_room_turn_for_input(db: Session,room_id: UUID,actor_id: str,generation: str) -> bool:
    """추가 입력 대기는 actor 소유권을 유지하고 같은 실행의 중복 정산을 막는다."""
    room=lock_current_room_turn(db,room_id,actor_id,generation)
    if room is None:return False
    metadata=deepcopy(room.metadata_json)
    metadata[_TURN_KEY]["phase"]="waiting_input"
    metadata[_TURN_KEY]["waiting_expires_at"]=_waiting_expiry(db,room)
    _save(db,room,metadata)
    return True


def release_room_turn(db: Session,room_id: UUID,actor_id: str,generation: str,reason: str) -> bool:
    """실제 종료/오류만 소유 세대를 해제한다. 대기는 종료로 취급하지 않는다."""
    if not isinstance(reason,str) or reason not in _TERMINAL_REASONS or not _actor_valid(actor_id):return False
    room=_locked_room(db,room_id)
    if room is None or not isinstance(room.metadata_json,dict):return False
    if not _matches(room.metadata_json.get(_TURN_KEY),actor_id,generation):return False
    metadata=deepcopy(room.metadata_json)
    metadata.pop(_TURN_KEY)
    _save(db,room,metadata)
    return True
