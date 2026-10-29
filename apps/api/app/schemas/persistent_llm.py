"""서버 내부에서 생성하는 지속 대화·예약 계약. 클라이언트 소유 증명으로 사용하지 않는다."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TurnScope:
    org_id: UUID
    group_id: UUID
    bot_id: UUID
    room_id: UUID
    principal_key: str
    generation: int
    version_id: UUID


@dataclass(frozen=True)
class MutationToken:
    version_id: UUID
    token: UUID
    fence: int


@dataclass(frozen=True)
class BudgetQuote:
    account_ids: tuple[UUID, ...]
    currency: str
    tariff_version: str
    amount_upper: int
    input_tokens_upper: int
    output_tokens_upper: int
    materialized_input_hash: str


@dataclass(frozen=True)
class UsageEvidence:
    actual_amount: int
    currency: str
    tariff_version: str
    receipt_ref: str
