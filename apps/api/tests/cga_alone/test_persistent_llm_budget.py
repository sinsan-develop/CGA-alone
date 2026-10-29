"""여러 방의 공용 예산 예약·불명 hold·중복 정산을 실제 PG로 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from app.models import ChannelQueueEvent, ChannelRoom, LlmBudgetAccount
from app.schemas.persistent_llm import BudgetQuote, UsageEvidence
from app.services.llm_budget import (
    BudgetConflict,
    reserve_turn_budget,
    settle_turn_budget,
    hold_unknown_budget,
    mark_turn_submitted,
    release_presubmit_budget,
)


def setup(c, amount=100, count=3, scopes=1):
    with Session(c.engine) as db:
        ids = []
        for index in range(scopes):
            a = LlmBudgetAccount(
                scope_kind="experiment" if index == 0 else "organization",
                scope_key=str(c.org_id) + str(index),
                currency="USD",
                tariff_version="test-v1",
                limit_amount=amount,
                limit_count=count,
            )
            db.add(a)
            db.flush()
            ids.append(a.id)
        events = []
        for _ in range(2):
            room = ChannelRoom(
                channel_type="simulator",
                client_room_id=str(uuid4()),
                bot_id=c.bot_id,
                bot_version_id=c.version_id,
                participant_id=str(c.user_id),
            )
            db.add(room)
            db.flush()
            event = ChannelQueueEvent(
                channel_type="simulator",
                room_id=room.id,
                bot_id=c.bot_id,
                bot_version_id=c.version_id,
                operation_kind="persistent_llm",
            )
            db.add(event)
            db.flush()
            events.append(event.id)
        db.commit()
    quote = BudgetQuote(tuple(ids), "USD", "test-v1", 30, 100, 20, "a" * 64)
    return ids, events, quote


def test_duplicate_event_reserves_once(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        db.commit()
        reserve_turn_budget(db, events[0], quote)
        db.commit()
        a = db.get(LlmBudgetAccount, ids[0])
        assert (a.reserved_amount, a.reserved_count) == (30, 1)


def test_concurrent_rooms_reserve_last_budget_once(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c, amount=30, count=1)
    barrier = Barrier(2)

    def reserve(event):
        with Session(c.engine) as db:
            barrier.wait(timeout=10)
            try:
                reserve_turn_budget(db, event, quote)
                db.commit()
                return "allowed"
            except BudgetConflict:
                db.rollback()
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, events)) == ["allowed", "blocked"]


def test_global_and_org_caps_are_atomic(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c, scopes=2)
    with Session(c.engine) as db:
        db.get(LlmBudgetAccount, ids[1]).limit_amount = 10
        db.commit()
        with pytest.raises(BudgetConflict):
            reserve_turn_budget(db, events[0], quote)
        db.rollback()
        assert all(db.get(LlmBudgetAccount, i).reserved_amount == 0 for i in ids)


def test_unknown_holds_money_and_count(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c, amount=30, count=1)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        mark_turn_submitted(db, events[0])
        hold_unknown_budget(db, events[0])
        db.commit()
        a = db.get(LlmBudgetAccount, ids[0])
        assert (a.reserved_amount, a.reserved_count) == (30, 1)
        with pytest.raises(BudgetConflict):
            reserve_turn_budget(db, events[1], quote)


def test_settlement_is_idempotent(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        mark_turn_submitted(db, events[0])
        db.commit()
        evidence = UsageEvidence(12, "USD", "test-v1", "synthetic-receipt")
        settle_turn_budget(db, events[0], evidence)
        db.commit()
        settle_turn_budget(db, events[0], evidence)
        db.commit()
        a = db.get(LlmBudgetAccount, ids[0])
        assert (a.spent_amount, a.reserved_amount, a.spent_count, a.reserved_count) == (
            12,
            0,
            1,
            0,
        )


def test_only_proven_presubmit_failure_refunds(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        release_presubmit_budget(db, events[0])
        db.commit()
        a = db.get(LlmBudgetAccount, ids[0])
        assert (a.reserved_amount, a.reserved_count, a.spent_count) == (0, 0, 0)
        reserve_turn_budget(db, events[1], quote)
        mark_turn_submitted(db, events[1])
        db.commit()
        with pytest.raises(BudgetConflict):
            release_presubmit_budget(db, events[1])


@pytest.mark.parametrize("change", [{"currency": "KRW"}, {"tariff_version": "unknown"}])
def test_currency_or_tariff_unknown_rejects(change, persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        with pytest.raises(BudgetConflict):
            reserve_turn_budget(db, events[0], replace(quote, **change))


def test_reserved_tariff_is_frozen_until_settlement(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        mark_turn_submitted(db, events[0])
        db.commit()
        db.get(LlmBudgetAccount, ids[0]).tariff_version = "test-v2"
        db.commit()
        settle_turn_budget(
            db, events[0], UsageEvidence(12, "USD", "test-v1", "frozen-tariff")
        )
        db.commit()
        assert db.get(LlmBudgetAccount, ids[0]).spent_amount == 12


@pytest.mark.parametrize("amount", [0.5, True])
def test_quote_rejects_non_integer_money(amount, persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        with pytest.raises(BudgetConflict):
            reserve_turn_budget(db, events[0], replace(quote, amount_upper=amount))


def test_actual_overrun_is_recorded_and_blocks_new_admission(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c, amount=100)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        mark_turn_submitted(db, events[0])
        db.commit()
        settle_turn_budget(
            db, events[0], UsageEvidence(40, "USD", "test-v1", "overrun-receipt")
        )
        db.commit()
        account = db.get(LlmBudgetAccount, ids[0])
        assert account.spent_amount == 40 and account.blocked
        with pytest.raises(BudgetConflict):
            reserve_turn_budget(db, events[1], quote)


def test_duplicate_submit_cannot_authorize_second_paid_post(persistent_context):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        assert mark_turn_submitted(db, events[0]) is True
        db.commit()
        assert mark_turn_submitted(db, events[0]) is False
