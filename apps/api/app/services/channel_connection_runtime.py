"""검증된 연결 실행부만 기존 방/참가자 증명/CGA 실행에 연결한다."""
from __future__ import annotations
from dataclasses import dataclass, field
import hashlib,hmac
from uuid import UUID,uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.models import ChannelConnection,ChannelConnectionRoom,ChannelConnectionDelivery,ChannelRoom,ChannelMessage,ChannelQueueEvent
from app.services import channel_connection_state as state
from app.services.channel_connections import _cipher, encrypt_connection_state, decrypt_connection_state


@dataclass(frozen=True)
class ConnectionMembership:
    room_id: UUID
    participant_id: str
    conversation_token: str=field(repr=False)


def _room_reservation(connection,event):
    _cipher(settings.channel_connection_encryption_key)
    raw=f"cga-connected-room-v1:{connection.organization_id}:{connection.id}:{event['nativeRoomKey']}".encode()
    digest=hmac.new(settings.channel_connection_encryption_key.encode(),raw,hashlib.sha256).hexdigest()
    return "wc-auth:cx:"+digest


def ensure_connection_membership(sessions,connection_id,installation_id,owner,fence,event,request,*,expected_generation=None):
    from app.api.routes import channels,webchat
    state._event(event)
    with sessions() as db:
        connection=state._live(db,connection_id,installation_id,owner,fence)
        if expected_generation is not None and connection.generation!=expected_generation:raise HTTPException(409,"CONNECTION_CHANGED")
        channel=connection.provider.replace("_","-")
        bot,version,_=channels._get_active_bot_version(db,str(connection.bot_id),channel)
        room_hash=state.scoped_hash(event["nativeRoomKey"])
        mapping=db.scalar(select(ChannelConnectionRoom).where(ChannelConnectionRoom.connection_id==connection.id,ChannelConnectionRoom.native_room_hash==room_hash).with_for_update())
        room=db.get(ChannelRoom,mapping.room_id) if mapping else None
        fresh=room is None or room.status!="open" or room.deleted_at is not None
        if fresh:
            reservation=_room_reservation(connection,event)
            room=db.scalar(select(ChannelRoom).where(ChannelRoom.channel_type==channel,ChannelRoom.client_room_id==reservation,ChannelRoom.deleted_at.is_(None)))
            if room is None:
                result=channels.create_channel_room(channel,channels.ChannelRoomCreateRequest(bot_id=str(bot.id),client_room_id=reservation,
                    participant_name=event.get("participantName") or event["nativeParticipantKey"][:120],start_immediately=False),request,settings.webchat_api_key)["data"]
                room=db.get(ChannelRoom,UUID(result["room"]["id"]))
            if room.bot_id!=bot.id or room.channel_type!=channel:raise HTTPException(409,"CONNECTED_ROOM_SCOPE_MISMATCH")
            # 방 예약 후 map/증명 저장 전 crash만 재사용한다. 이미 발급된 타인의 증명을 채택하지 않는다.
            if "__webchatAccess" in (room.metadata_json or {}):raise HTTPException(409,"CONNECTED_ROOM_BINDING_CONFLICT")
            if mapping is None:
                mapping=ChannelConnectionRoom(id=uuid4(),connection_id=connection.id,native_room_hash=room_hash,room_id=room.id)
                db.add(mapping)
            else:
                mapping.room_id=room.id;mapping.participant_ciphertext=None;mapping.credential_key_version=None
            binding={"mode":event["roomMode"],"bot_id":str(bot.id),"organization_id":str(bot.organization_id),"channel":channel,
                "client_room_hash":state.scoped_hash(str(room.id)),"members":{}}
            members={}
        else:
            room,binding=webchat._shared_room(db,bot,room.id,lock=True,channel=channel,allow_private=True)
            if binding.get("mode")!=event["roomMode"]:raise HTTPException(409,"CONNECTED_ROOM_MODE_MISMATCH")
            members=decrypt_connection_state(mapping.participant_ciphertext,connection.organization_id,connection.id,settings.channel_connection_encryption_key,mapping.credential_key_version,"room:"+str(mapping.id)) if mapping.participant_ciphertext else {}
        actor_key=state.scoped_hash(event["nativeParticipantKey"])
        previous=members.get(actor_key)
        if event["roomMode"]=="private" and members and actor_key not in members:raise HTTPException(409,"PRIVATE_NATIVE_ACTOR_MISMATCH")
        if isinstance(previous,dict):
            if previous.get("kind")!=event["participantKind"]:raise HTTPException(409,"NATIVE_ACTOR_KIND_CHANGED")
            try:
                webchat._shared_member(db,bot,room.id,previous["token"],channel=channel,allow_private=True)
                token,participant=previous["token"],previous["participant_id"]
            except HTTPException:
                previous=None
        if not isinstance(previous,dict):
            issued=webchat._issue_shared_member(db,room,binding,bot,version,event.get("participantName") or event["nativeParticipantKey"][:120],event["participantKind"],commit=False)
            token,participant=issued["conversationToken"],issued["participantId"]
            members[actor_key]={"token":token,"participant_id":participant,"kind":event["participantKind"]}
            mapping.participant_ciphertext=encrypt_connection_state(members,connection.organization_id,connection.id,settings.channel_connection_encryption_key,settings.channel_connection_key_version,"room:"+str(mapping.id))
            mapping.credential_key_version=settings.channel_connection_key_version
        db.flush();db.commit()
        return ConnectionMembership(room.id,participant,token)


def _restore_selection(db,channel,room_id,event):
    if event.get("choiceIndex") is None:return event["message"]
    from app.api.routes import channels
    from app.services.channel_connection_bridge import convert_channel_packet
    try:source_id=UUID(event["sourceMessageId"])
    except (ValueError,KeyError,TypeError):raise HTTPException(422,"INVALID_NATIVE_SELECTION") from None
    source=db.scalar(select(ChannelMessage).where(ChannelMessage.id==source_id,ChannelMessage.room_id==room_id,ChannelMessage.participant_kind=="bot",ChannelMessage.deleted_at.is_(None)))
    if source is None:raise HTTPException(409,"NATIVE_SELECTION_SOURCE_MISSING")
    return convert_channel_packet(channel,"selection",{"source":channels._serialize_message(source),"event":event})


def execute_connection_delivery(sessions,delivery_id,installation_id,owner,fence,execution_claim_id,request):
    from app.api.routes import channels,webchat
    started=False
    try:
        with sessions() as db:
            initial=db.get(ChannelConnectionDelivery,delivery_id)
            if initial is None:raise HTTPException(404,"CONNECTION_DELIVERY_NOT_FOUND")
            connection=state._live(db,initial.connection_id,installation_id,owner,fence)
            delivery=state._delivery(db,delivery_id)
            if (delivery.status!="executing" or delivery.execution_claim_id!=execution_claim_id or delivery.generation!=connection.generation
                or delivery.deadline_at is None or delivery.deadline_at<=state.clock(db)):
                raise HTTPException(409,"EXECUTION_CLAIM_LOST")
            event=dict(delivery.event_json);generation=delivery.generation;connection_id=connection.id;db.commit()
        membership=ensure_connection_membership(sessions,connection_id,installation_id,owner,fence,event,request,expected_generation=generation)
        with sessions() as db:
            connection=state._live(db,connection_id,installation_id,owner,fence);delivery=state._delivery(db,delivery_id)
            if connection.generation!=generation or delivery.status!="executing" or delivery.execution_claim_id!=execution_claim_id:
                raise HTTPException(409,"EXECUTION_CLAIM_LOST")
            channel=connection.provider.replace("_","-")
            bot,version,_=channels._get_active_bot_version(db,str(connection.bot_id),channel)
            message=_restore_selection(db,channel,membership.room_id,event)
            if channel in {"slack","discord","telegram","google-chat","line"}:
                # 실제 CGA 진입 전 확실히 거절한 카드를 유료 결과불명으로 오인하지 않는다.
                webchat._validate_native_card_response(db,db.get(ChannelRoom,membership.room_id),version,membership.participant_id,message,channel)
            delivery.room_id=membership.room_id
            db.expunge(bot);db.expunge(version);db.commit()
        payload=channels.ChannelMessageRequest(message=message,participant_id=membership.participant_id,addressed=event["addressed"],source_talk_node_id=event.get("sourceTalkNodeId"))
        started=True
        result=webchat._shared_runtime_message(bot,version,str(membership.room_id),payload,request,settings.webchat_api_key,membership.conversation_token,
            channel=channel,allow_private=True,event_id=state.scoped_hash(event["eventId"]))["data"]
        with sessions() as db:
            delivery=state.mark_ready(db,delivery_id,result,execution_claim_id)
            user_id=(result.get("userMessage") or {}).get("id")
            if user_id:
                message=db.get(ChannelMessage,UUID(user_id))
                if message is None or message.room_id!=membership.room_id:raise HTTPException(409,"CGA_RESULT_SCOPE_MISMATCH")
                delivery.request_message_id=message.id
                delivery.queue_event_id=db.scalar(select(ChannelQueueEvent.id).where(ChannelQueueEvent.request_message_id==message.id,ChannelQueueEvent.room_id==membership.room_id).order_by(ChannelQueueEvent.created_at).limit(1))
            db.commit()
        return result
    except Exception as error:
        with sessions() as db:
            delivery=state._delivery(db,delivery_id)
            if delivery.execution_claim_id==execution_claim_id and delivery.status in {"executing","unknown"} and delivery.send_claim_id is None:
                busy=isinstance(error,HTTPException) and error.status_code==409 and error.detail=="이 방의 다른 질문이 진행 중입니다."
                delivery.status="unknown" if started and not busy else "failed"
                delivery.last_error_code="EXECUTION_UNKNOWN" if delivery.status=="unknown" else "ROOM_BUSY" if busy else "EXECUTION_NOT_STARTED"
                db.commit()
        raise
