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


@pytest.mark.parametrize(
    "change",
    ["actual_overrun", "currency", "unit", "tariff", "amount_limit", "count_limit"],
)
def test_reserved_but_unsubmitted_turn_rechecks_account_safety(
    change, persistent_context
):
    c = persistent_context
    ids, events, quote = setup(c)
    with Session(c.engine) as db:
        reserve_turn_budget(db, events[0], quote)
        reserve_turn_budget(db, events[1], quote)
        db.commit()
        if change == "actual_overrun":
            assert mark_turn_submitted(db, events[0]) is True
            db.commit()
            settle_turn_budget(
                db,
                events[0],
                UsageEvidence(40, "USD", "test-v1", "overrun-before-next-submit"),
            )
        else:
            account = db.get(LlmBudgetAccount, ids[0])
            if change == "currency":
                account.currency = "KRW"
            elif change == "unit":
                account.unit_scale = 1000
            elif change == "tariff":
                account.tariff_version = "test-v2"
            elif change == "amount_limit":
                account.limit_amount = 50
            elif change == "count_limit":
                account.limit_count = 1
        db.commit()
        with pytest.raises(BudgetConflict):
            mark_turn_submitted(db, events[1])
        db.rollback()
        event = db.get(ChannelQueueEvent, events[1])
        assert event.submitted_at is None and event.phase == "reserved"
        # 호출 전 실패가 입증된 이 예약만 환급할 수 있고 이미 제출한 요청은 건드리지 않는다.
        release_presubmit_budget(db, events[1])
        db.commit()


BUDGET_PROCESS_WORKER = r"""
import os,sys,json,time
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.schemas.persistent_llm import BudgetQuote
from app.services.llm_budget import reserve_turn_budget,BudgetConflict
identifier,quote_json,barrier=sys.argv[1:]
print(json.dumps({'worker_pid':os.getpid()}),flush=True)
url=os.environ['AIDOT_TEST_DATABASE_URL'];assert url.rsplit('/',1)[-1]=='cga_alone_qa'
engine=create_engine(url,pool_pre_ping=True)
values=json.loads(quote_json);values['account_ids']=tuple(UUID(x) for x in values['account_ids']);quote=BudgetQuote(**values)
barrier=Path(barrier);barrier.mkdir(exist_ok=True)
(barrier/str(os.getpid())).touch();stop=time.monotonic()+10
while len(list(barrier.iterdir()))<2:
    if time.monotonic()>stop:raise RuntimeError('QA process barrier timeout')
    time.sleep(.01)
with Session(engine) as db:
    try:
        reserve_turn_budget(db,UUID(identifier),quote);db.commit();print('allowed')
    except BudgetConflict:
        db.rollback();print('blocked')
engine.dispose()
"""


def budget_process(event_id, quote, barrier):
    from dataclasses import asdict
    import json, subprocess, sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            BUDGET_PROCESS_WORKER,
            str(event_id),
            json.dumps(asdict(quote), default=str),
            str(barrier),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    receipt = {
        "event_id": str(event_id),
        "quote": asdict(quote),
        "worker": json.loads(result.stdout.splitlines()[0]),
        "status": result.stdout.splitlines()[-1],
        "actual_openai_calls": 0,
    }
    directory = Path(__file__).parents[4] / ".local/pllm-p04-budget-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    (
        directory
        / (str(event_id) + "-" + str(receipt["worker"]["worker_pid"]) + ".json")
    ).write_text(json.dumps(receipt, default=str, indent=2), encoding="utf-8")
    return result.stdout.splitlines()[-1]


@pytest.mark.parametrize("duplicate", [False, True])
def test_real_process_global_and_org_last_budget_is_atomic(
    persistent_context, tmp_path, duplicate
):
    c = persistent_context
    ids, events, quote = setup(c, amount=30, count=1, scopes=2)
    selected = [events[0], events[0]] if duplicate else events
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda event: budget_process(event, quote, tmp_path), selected)
        )
    assert sorted(results) == (
        ["allowed", "allowed"] if duplicate else ["allowed", "blocked"]
    )
    with Session(c.engine) as db:
        accounts = [db.get(LlmBudgetAccount, account_id) for account_id in ids]
        assert all(
            row.reserved_amount == 30
            and row.reserved_count == 1
            and row.spent_count == 0
            for row in accounts
        )
        winner = events[0] if duplicate else events[results.index("allowed")]
        assert mark_turn_submitted(db, winner)
        hold_unknown_budget(db, winner)
        db.commit()
    loser = events[1] if duplicate else events[results.index("blocked")]
    assert budget_process(loser, quote, tmp_path) == "blocked"
    with Session(c.engine) as db:
        assert all(
            db.get(LlmBudgetAccount, account_id).reserved_count == 1
            for account_id in ids
        )
