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


from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PersistentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersistentRoomStartRequest(PersistentRequest):
    dialog_id: str = Field(min_length=1, max_length=150)
    room_id: UUID | None = None


class PersistentTurnRequest(PersistentRequest):
    expected_generation: int = Field(ge=1)
    message_key: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=4000)
    dialog_id: str = Field(min_length=1, max_length=150)


class PersistentAckRequest(PersistentRequest):
    message_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PersistentResetRequest(PersistentRequest):
    expected_generation: int = Field(ge=1)
    confirm_reset: Literal[True]
    dialog_id: str = Field(min_length=1, max_length=150)


class PersistentCloseRequest(PersistentRequest):
    expected_generation: int = Field(ge=1)


class PersistentRevokeRequest(PersistentRequest):
    reason: str = Field(min_length=1, max_length=2000)
    version_id: UUID | None = None
