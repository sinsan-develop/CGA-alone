"""비어 있는 전용 테스트 DB에서만 신규 schema의 가역성과 자료 보존 guard를 검사한다."""

from pathlib import Path
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text, delete
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models import LlmBudgetAccount

TABLES = (
    "channel_llm_bindings",
    "channel_llm_version_guards",
    "llm_budget_accounts",
    "llm_budget_reservations",
)
OLD_TABLES = (
    "channel_rooms",
    "channel_messages",
    "channel_queue_events",
    "bots",
    "bot_versions",
    "users",
)


def run_revision(engine, direction, revision):
    previous = settings.database_url
    test_url = engine.url.render_as_string(hide_password=False)
    assert test_url != str(settings.sqlalchemy_database_url)
    settings.database_url = test_url
    ini = Path(__file__).parents[2] / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "alembic"))
    config.config_file_name = None
    try:
        getattr(command, direction)(config, revision)
    finally:
        settings.database_url = previous


def revision(engine):
    with engine.connect() as db:
        return db.scalar(text("select version_num from alembic_version"))


def test_empty_upgrade_downgrade_preserves_legacy_queue(
    test_engine, migrated_test_database
):
    with test_engine.connect() as db:
        assert all(
            db.scalar(text("select count(*) from " + table)) == 0 for table in TABLES
        )
        assert (
            db.scalar(
                text(
                    "select count(*) from channel_queue_events where operation_kind <> 'channel'"
                )
            )
            == 0
        )
        before = {
            table: db.scalar(text("select count(*) from " + table))
            for table in OLD_TABLES
        }
    old_indexes = {
        item["name"]
        for item in inspect(test_engine).get_indexes("channel_queue_events")
    } - {"uq_queue_persistent_message"}
    assert {
        "idx_channel_queue_events_channel_type",
        "idx_channel_queue_events_created_at",
        "idx_channel_queue_events_room_id",
        "idx_channel_queue_events_status",
    }.issubset(old_indexes)
    try:
        run_revision(test_engine, "downgrade", "20261029_0028")
        assert revision(test_engine) == "20261029_0028"
        assert all(not inspect(test_engine).has_table(table) for table in TABLES)
        assert {
            item["name"]
            for item in inspect(test_engine).get_indexes("channel_queue_events")
        } == old_indexes
    finally:
        run_revision(test_engine, "upgrade", "20261029_0029")
    assert revision(test_engine) == "20261029_0029"
    assert all(inspect(test_engine).has_table(table) for table in TABLES)
    columns = {
        item["name"]: item
        for item in inspect(test_engine).get_columns("channel_queue_events")
    }
    assert (
        "channel" in columns["operation_kind"]["default"]
        and not columns["operation_kind"]["nullable"]
    )
    assert "queued" in columns["phase"]["default"]
    assert any(
        item["name"] == "fk_queue_llm_binding"
        for item in inspect(test_engine).get_foreign_keys("channel_queue_events")
    )
    with test_engine.connect() as db:
        assert {
            table: db.scalar(text("select count(*) from " + table))
            for table in OLD_TABLES
        } == before


def test_nonempty_ledger_refuses_destructive_downgrade(
    test_engine, migrated_test_database
):
    with Session(test_engine) as db:
        account = LlmBudgetAccount(
            scope_kind="migration_guard",
            scope_key=str(uuid4()),
            currency="USD",
            limit_amount=1,
            limit_count=1,
        )
        db.add(account)
        db.commit()
        identifier = account.id
    try:
        with pytest.raises(RuntimeError, match="지속 대화 자료"):
            run_revision(test_engine, "downgrade", "20261029_0028")
        assert revision(test_engine) == "20261029_0029"
        with Session(test_engine) as db:
            assert db.get(LlmBudgetAccount, identifier) is not None
    finally:
        with Session(test_engine) as db:
            db.execute(
                delete(LlmBudgetAccount).where(LlmBudgetAccount.id == identifier)
            )
            db.commit()
