"""실제 외부 호출 전에 commit하는 PG 공용 예약. unknown은 자동 환급하지 않는다."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib, json
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import ChannelQueueEvent, LlmBudgetAccount, LlmBudgetReservation
from app.schemas.persistent_llm import BudgetQuote, UsageEvidence


class BudgetConflict(ValueError):
    pass


def _event(db, event_id):
    event = db.scalar(
        select(ChannelQueueEvent)
        .where(ChannelQueueEvent.id == event_id, ChannelQueueEvent.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if event is None or event.operation_kind != "persistent_llm":
        raise BudgetConflict("지속 대화 event가 필요합니다.")
    return event


def _accounts(db, ids):
    rows = list(
        db.scalars(
            select(LlmBudgetAccount)
            .where(LlmBudgetAccount.id.in_(ids))
            .order_by(LlmBudgetAccount.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(rows) != len(set(ids)) or not rows:
        raise BudgetConflict("모든 예산 계정이 필요합니다.")
    return rows


def reserve_turn_budget(db: Session, event_id, quote: BudgetQuote):
    event = _event(db, event_id)
    if (
        not quote.account_ids
        or len(set(quote.account_ids)) != len(quote.account_ids)
        or type(quote.amount_upper) is not int
        or quote.amount_upper < 0
        or type(quote.input_tokens_upper) is not int
        or type(quote.output_tokens_upper) is not int
        or quote.input_tokens_upper < 1
        or quote.output_tokens_upper < 1
        or len(quote.materialized_input_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in quote.materialized_input_hash)
        or not quote.tariff_version
    ):
        raise BudgetConflict("검증된 요청 상한·요금표가 필요합니다.")
    accounts = _accounts(db, quote.account_ids)
    raw = {**asdict(quote), "account_ids": sorted(str(x) for x in quote.account_ids)}
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    existing = list(
        db.scalars(
            select(LlmBudgetReservation)
            .where(LlmBudgetReservation.event_id == event_id)
            .execution_options(populate_existing=True)
        ).all()
    )
    if existing:
        if {r.account_id for r in existing} != set(quote.account_ids) or any(
            r.quote_hash != digest for r in existing
        ):
            raise BudgetConflict("중복 요청의 예약 계약이 다릅니다.")
        return existing
    if event.submitted_at is not None or event.phase not in (
        "queued",
        "admitted",
        "reserved",
    ):
        raise BudgetConflict("외부 제출 가능성이 있는 event를 재예약할 수 없습니다.")
    if len({a.unit_scale for a in accounts}) != 1 or any(
        a.unit_scale < 1 for a in accounts
    ):
        raise BudgetConflict("예산 금액 단위가 일치하지 않습니다.")
    for account in accounts:
        if (
            account.blocked
            or account.currency != quote.currency
            or account.tariff_version != quote.tariff_version
        ):
            raise BudgetConflict("통화·요금표·차단 상태 확인이 필요합니다.")
        if (
            account.spent_amount + account.reserved_amount + quote.amount_upper
            > account.limit_amount
            or account.spent_count + account.reserved_count + 1 > account.limit_count
        ):
            raise BudgetConflict("공용 예산 또는 횟수 한도입니다.")
    group = uuid4()
    rows = []
    for account in accounts:
        account.reserved_amount += quote.amount_upper
        account.reserved_count += 1
        row = LlmBudgetReservation(
            event_id=event_id,
            account_id=account.id,
            reservation_group_id=group,
            quote_hash=digest,
            amount_upper=quote.amount_upper,
            state="reserved",
            usage_json={
                "quote": {
                    "currency": quote.currency,
                    "tariff_version": quote.tariff_version,
                    "unit_scale": account.unit_scale,
                    "input_tokens_upper": quote.input_tokens_upper,
                    "output_tokens_upper": quote.output_tokens_upper,
                    "materialized_input_hash": quote.materialized_input_hash,
                }
            },
        )
        db.add(row)
        rows.append(row)
    event.phase = "reserved"
    db.flush()
    return rows


def _locked_reservations(db, event_id):
    event = _event(db, event_id)
    rows = list(
        db.scalars(
            select(LlmBudgetReservation)
            .where(LlmBudgetReservation.event_id == event_id)
            .execution_options(populate_existing=True)
        ).all()
    )
    if not rows:
        raise BudgetConflict("기존 예약이 필요합니다.")
    accounts = {a.id: a for a in _accounts(db, [r.account_id for r in rows])}
    return event, rows, accounts


def mark_turn_submitted(db: Session, event_id):
    event, rows, accounts = _locked_reservations(db, event_id)
    if event.submitted_at is not None:
        return False
    if any(r.state != "reserved" for r in rows):
        raise BudgetConflict("제출할 수 없는 예약입니다.")
    # 예약 후 다른 방의 초과 정산·정책 변경도 최초 유료 제출 전에 반영한다.
    for row in rows:
        account = accounts[row.account_id]
        frozen = (row.usage_json or {}).get("quote", {})
        if (
            account.blocked
            or account.currency != frozen.get("currency")
            or account.tariff_version != frozen.get("tariff_version")
            or account.unit_scale != frozen.get("unit_scale")
            or account.spent_amount + account.reserved_amount > account.limit_amount
            or account.spent_count + account.reserved_count > account.limit_count
        ):
            raise BudgetConflict("제출 전 공용 예산·요금표·차단 상태가 변경되었습니다.")
    event.submitted_at = datetime.now(timezone.utc)
    event.phase = "submitted"
    for row in rows:
        row.state = "submitted"
    db.flush()
    return True


def hold_unknown_budget(db: Session, event_id):
    event, rows, _ = _locked_reservations(db, event_id)
    if any(r.state not in ("reserved", "submitted", "unknown") for r in rows):
        raise BudgetConflict("불명 상태로 전환할 수 없습니다.")
    event.submitted_at = event.submitted_at or datetime.now(timezone.utc)
    event.phase = "unknown"
    for row in rows:
        row.state = "unknown"
    db.flush()


def settle_turn_budget(db: Session, event_id, evidence: UsageEvidence):
    event, rows, accounts = _locked_reservations(db, event_id)
    if (
        type(evidence.actual_amount) is not int
        or evidence.actual_amount < 0
        or not evidence.receipt_ref
        or len(evidence.receipt_ref) > 200
    ):
        raise BudgetConflict("확정된 사용량 증거가 필요합니다.")
    for row in rows:
        frozen = (row.usage_json or {}).get("quote", {})
        account = accounts[row.account_id]
        if (
            frozen.get("currency") != evidence.currency
            or frozen.get("tariff_version") != evidence.tariff_version
            or account.currency != frozen.get("currency")
            or account.unit_scale != frozen.get("unit_scale")
        ):
            raise BudgetConflict("예약 시점의 통화·요금표·금액 단위와 정산이 다릅니다.")
    if all(r.state == "settled" for r in rows):
        if any(
            r.actual_amount != evidence.actual_amount
            or r.receipt_ref != evidence.receipt_ref
            for r in rows
        ):
            raise BudgetConflict("정산 증거가 달라졌습니다.")
        return rows
    if any(r.state not in ("submitted", "unknown") for r in rows):
        raise BudgetConflict("제출된 예약만 정산합니다.")
    for row in rows:
        account = accounts[row.account_id]
        account.reserved_amount -= row.amount_upper
        account.reserved_count -= 1
        account.spent_amount += evidence.actual_amount
        account.spent_count += 1
        if (
            evidence.actual_amount > row.amount_upper
            or account.spent_amount + account.reserved_amount > account.limit_amount
        ):
            account.blocked = True
            account.blocked_reason = "actual_usage_exceeded_reserved_upper"
        row.state = "settled"
        row.actual_amount = evidence.actual_amount
        row.receipt_ref = evidence.receipt_ref
        row.settled_at = datetime.now(timezone.utc)
    db.flush()
    return rows


def release_presubmit_budget(db: Session, event_id):
    event, rows, accounts = _locked_reservations(db, event_id)
    if event.submitted_at is not None or any(
        r.state not in ("reserved", "released") for r in rows
    ):
        raise BudgetConflict("제출 가능성/불명 예약을 환급할 수 없습니다.")
    for row in rows:
        if row.state == "released":
            continue
        account = accounts[row.account_id]
        account.reserved_amount -= row.amount_upper
        account.reserved_count -= 1
        row.state = "released"
        row.settled_at = datetime.now(timezone.utc)
    event.phase = "failed_presubmit"
    db.flush()
