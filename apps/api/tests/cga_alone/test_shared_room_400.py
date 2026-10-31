"""S13 400개 서버 발급 참가자 증명의 합성 라우팅. 실제 메신저 400인 성능 시험이 아니다."""
from uuid import UUID
from sqlalchemy import select, func
from app.models import ChannelRoom, ChannelMessage, ChannelQueueEvent
from tests.cga_alone.test_client_contract import client_context, create, send, member_headers
from tests.cga_alone.test_webchat_credentials import credential_context
from tests.cga_alone.test_two_stage_runtime import native_secondary_document
from tests.cga_alone.test_secondary_llm_config import secondary_bot


def test_400_participants_keep_one_questioner_and_general_chat(client_context):
    c,client=client_context;first=create(client_context);members=[first]
    for index in range(1,400):
        joined=client.post(f"/api/v1/channels/webchat/rooms/{first['roomId']}/participants",headers={"X-Aidot-Webchat-Key":"s04-qa-key"},json={"participant_name":f"합성 참가자 {index:03d}"})
        assert joined.status_code==200,joined.text
        members.append({**joined.json()["data"],"room":first["room"]})
    assert len({item['participantId'] for item in members})==400
    assert len({item['conversationToken'] for item in members})==400
    opened=send(client_context,first,"대표 질문",event="s13-first");assert opened.status_code==200
    turn=opened.json()['data']['turn'];assert turn['ownerParticipantId']==first['participantId'] and turn['state']=='waiting_input'
    blocked=0
    for index,member in enumerate(members[1:],1):
        attempt=send(client_context,member,f"다른 질문 {index}");assert attempt.status_code==409;blocked+=1
        general=send(client_context,member,f"일반 대화 {index:03d}",addressed=False)
        assert general.status_code==200 and general.json()['data']['botMessages']==[]
        assert general.json()['data']['turn']['ownerParticipantId']==first['participantId']
        assert general.json()['data']['turn']['canAskBot'] is False
    assert blocked==399 and c[3]==[]
    c[2].expire_all();room=c[2].get(ChannelRoom,UUID(first['roomId']))
    assert len(room.metadata_json['__webchatAccess']['members'])==400
    assert room.metadata_json['__roomTurn']['actor_id']==first['participantId']
    assert c[2].scalar(select(func.count()).select_from(ChannelQueueEvent).where(ChannelQueueEvent.room_id==room.id))==1
    stored=list(c[2].scalars(select(ChannelMessage).where(ChannelMessage.room_id==room.id)))
    assert len(stored)==401
    assert {item.text for item in stored if item.text.startswith('일반 대화 ')}=={f'일반 대화 {i:03d}' for i in range(1,400)}
    assert len({item.participant_id for item in stored if item.participant_kind=='user'})==400
    # 같은 봇의 다른 공유방/개인방은 첫 방이 응답 대기 중이어도 진행한다.
    other=create(client_context);private=create(client_context,mode='private')
    assert send(client_context,other,'다른 방 질문').status_code==200
    assert send(client_context,private,'개인 질문').status_code==200
    done=send(client_context,private,'개인 추가 설명');assert done.status_code==200 and done.json()['data']['runtime']['dialogEnded']
    assert len(c[3])==1
    state=client.get(f"/api/v1/channels/webchat/rooms/{first['roomId']}",headers=member_headers(members[-1]));assert state.status_code==200
    assert state.json()['data']['turn']['ownerParticipantId']==first['participantId'] and not state.json()['data']['turn']['canAskBot']
    completed=send(client_context,first,'원 질문자의 추가 설명');assert completed.status_code==200
    assert completed.json()['data']['runtime']['dialogEnded'] and completed.json()['data']['turn']['state']=='idle' and len(c[3])==2
    next_question=send(client_context,members[-1],'다음 질문');assert next_question.status_code==200
    assert next_question.json()['data']['turn']['ownerParticipantId']==members[-1]['participantId'] and len(c[3])==2
