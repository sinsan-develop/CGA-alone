"""기존 전용 PostgreSQL/rollback fixture를 그대로 사용한다."""
from tests.integration.conftest import db_session, test_engine
from tests.integration.test_bot_persistence_regression import migrated_test_database, regression_actor


import pytest


@pytest.fixture
def persistent_context(test_engine, migrated_test_database):
    """별도 PG 연결에서 경쟁할 수 있도록 합성 자료를 commit하고 정확한 ID로 정리한다."""
    from types import SimpleNamespace
    from uuid import uuid4
    from sqlalchemy import delete, select
    from sqlalchemy.orm import Session
    from app.core.version_documents import build_default_version_document
    from app.models import (Organization, Group, User, Bot, BotVersion, ChannelRoom, ChannelMessage, ChannelQueueEvent, AuditLog,
                            ChannelLlmBinding, ChannelLlmVersionGuard, LlmBudgetAccount, LlmBudgetReservation, VersionDialogAsset, VersionDialogFlowGraph)
    from app.schemas.persistent_llm import TurnScope
    from app.services.version_pins import register_sample_version
    with Session(test_engine) as db:
        org=Organization(name="지속대화 검증",code="pllm-"+uuid4().hex);db.add(org);db.flush()
        group=Group(organization_id=org.id,name="검증그룹",code="pllm-"+uuid4().hex);db.add(group);db.flush()
        user=User(organization_id=org.id,group_id=group.id,login_id="pllm"+uuid4().hex[:20],password_hash="test-only",name="검증 제작자",status="active");db.add(user);db.flush()
        bot=Bot(organization_id=org.id,group_id=group.id,created_by=user.id,name="지속대화 검증봇",data_json={});db.add(bot);db.flush()
        version=BotVersion(bot_id=bot.id,version_no=1,name="v1",status="draft",created_by=user.id,version_json=build_default_version_document());db.add(version);db.flush()
        room=ChannelRoom(channel_type="simulator",client_room_id=str(uuid4()),bot_id=bot.id,bot_version_id=version.id,participant_id=str(user.id));db.add(room);db.flush()
        scope=TurnScope(org.id,group.id,bot.id,room.id,"user:"+str(user.id),1,version.id)
        register_sample_version(db,version.id)
        db.commit()
        ctx=SimpleNamespace(scope=scope,org_id=org.id,group_id=group.id,user_id=user.id,bot_id=bot.id,version_id=version.id,room_id=room.id,engine=test_engine)
    try:yield ctx
    finally:
        with Session(test_engine) as db:
            events=select(ChannelQueueEvent.id).where(ChannelQueueEvent.bot_id==ctx.bot_id)
            accounts=select(LlmBudgetAccount.id).where(LlmBudgetAccount.scope_key.contains(str(ctx.org_id)))
            db.execute(delete(LlmBudgetReservation).where((LlmBudgetReservation.event_id.in_(events)) | (LlmBudgetReservation.account_id.in_(accounts))))
            db.execute(delete(ChannelQueueEvent).where(ChannelQueueEvent.bot_id==ctx.bot_id))
            db.execute(delete(ChannelMessage).where(ChannelMessage.room_id.in_(select(ChannelRoom.id).where(ChannelRoom.bot_id==ctx.bot_id))))
            db.execute(delete(ChannelLlmBinding).where(ChannelLlmBinding.bot_id==ctx.bot_id))
            db.execute(delete(ChannelLlmVersionGuard).where(ChannelLlmVersionGuard.version_id.in_(select(BotVersion.id).where(BotVersion.bot_id==ctx.bot_id))))
            db.execute(delete(LlmBudgetAccount).where(LlmBudgetAccount.scope_key.contains(str(ctx.org_id))))
            db.execute(delete(ChannelRoom).where(ChannelRoom.bot_id==ctx.bot_id))
            versions=select(BotVersion.id).where(BotVersion.bot_id==ctx.bot_id)
            db.execute(delete(VersionDialogFlowGraph).where(VersionDialogFlowGraph.version_id.in_(versions)))
            db.execute(delete(VersionDialogAsset).where(VersionDialogAsset.version_id.in_(versions)))
            db.execute(delete(BotVersion).where(BotVersion.bot_id==ctx.bot_id))
            db.execute(delete(Bot).where(Bot.id==ctx.bot_id))
            db.execute(delete(AuditLog).where(AuditLog.actor_user_id==ctx.user_id))
            db.execute(delete(User).where(User.id==ctx.user_id))
            db.execute(delete(Group).where(Group.id==ctx.group_id))
            db.execute(delete(Organization).where(Organization.id==ctx.org_id));db.commit()
