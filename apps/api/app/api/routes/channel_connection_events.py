"""Microsoft가 호출하는 단일 수신 주소. 사용자 JWT/공유 채널 키는 권한이 아니다."""
import json
from fastapi import APIRouter,HTTPException,Request,Response
from app.core.config import settings
from app.services.channel_connection_supervisor import ADAPTERS

router=APIRouter(prefix='/channel-connections',tags=['channel-connection-events'])


@router.post('/ms-teams/events')
async def teams_activity(request:Request):
    adapter=ADAPTERS.get('ms_teams')
    if not settings.channel_connection_worker_enabled or not settings.channel_connection_encryption_key or adapter is None:
        raise HTTPException(503,'TEAMS_RECEIVER_DISABLED')
    limit=256*1024
    try:
        declared=request.headers.get('content-length')
        if declared is not None and int(declared)>limit:raise HTTPException(413,'TEAMS_ACTIVITY_TOO_LARGE')
    except ValueError:raise HTTPException(400,'TEAMS_BODY_INVALID') from None
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>limit:raise HTTPException(413,'TEAMS_ACTIVITY_TOO_LARGE')
        body.extend(chunk)
    try:
        value=json.loads(body)
        if not isinstance(value,dict):raise ValueError()
    except (ValueError,TypeError,UnicodeError):raise HTTPException(422,'TEAMS_BODY_INVALID') from None
    await adapter.receive(request.headers.get('authorization',''),value)
    # Activity를 원장에 commit했거나 질문이 아닌 유효한 event임을 확인한 뒤에만 ACK한다.
    return Response(content='{}',status_code=200,media_type='application/json')
