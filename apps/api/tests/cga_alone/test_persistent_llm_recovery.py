"""PG 상태·확정 표출과 원격 이력 대역을 대조한다. 실제 모델 호출 없음."""

from copy import deepcopy
from dataclasses import replace
import json
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import (
    ChannelLlmBinding,
    ChannelQueueEvent,
    ChannelMessage,
    LlmBudgetAccount,
    LlmBudgetReservation,
)
from app.schemas.persistent_llm import BudgetQuote, UsageEvidence
from app.services.version_pins import open_pinned_dialog
from app.services.openai_conversations import OpenAIConversations, payload_hash
from app.services.persistent_llm import PersistentLlmService, TurnConflict
from tests.cga_alone.test_persistent_llm_provider import CONFIG, response, item


class MemoryTransport:
    def __init__(self):
        self.items = []
        self.calls = []
        self.generations = 0
        self.appends = 0
        self.response_mode = "normal"
        self.append_mode = "normal"
        self.cancel_unknown = False
        self.create_unknown = False

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append((method, url, deepcopy(payload)))
        if method == "POST" and url.endswith("/conversations"):
            if self.create_unknown:
                raise TimeoutError("conversation create unknown")
            return {"id": "conv_test"}
        if method == "GET" and "/items?" in url:
            return {"data": deepcopy(self.items), "has_more": False}
        if method == "POST" and url.endswith("/responses"):
            self.generations += 1
            if self.response_mode == "timeout":
                raise TimeoutError("synthetic response timeout")
            raw = response()
            raw["id"] = "resp_" + str(self.generations)
            if self.response_mode == "invalid":
                raw["output"][0]["content"][0]["text"] = "invalid JSON"
            if self.response_mode == "background":
                raw.update(status="in_progress", background=True, output=[], usage=None)
            return raw
        if method == "POST" and url.endswith("/items"):
            self.appends += 1
            if self.append_mode == "timeout_before":
                raise TimeoutError("ambiguous append")
            values = (
                payload["items"][:1]
                if self.append_mode == "partial"
                else payload["items"]
            )
            for value in values:
                self.items.append(
                    item(
                        "msg_" + str(len(self.items) + 1),
                        value["role"],
                        value["content"],
                    )
                )
            if self.append_mode == "timeout_after":
                raise TimeoutError("append result lost")
            return {"data": deepcopy(self.items[-len(values) :])}
        if method == "POST" and url.endswith("/cancel"):
            if self.cancel_unknown:
                raise TimeoutError("cancel unknown")
            return {
                "id": "resp_1",
                "status": "cancelled",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        if method == "GET" and "/responses/" in url:
            raw = response()
            raw["id"] = url.rsplit("/", 1)[-1]
            return raw
        raise AssertionError("unexpected synthetic request")


@pytest.fixture
def protocol(persistent_context):
    c = persistent_context
    t = MemoryTransport()
    client = OpenAIConversations(CONFIG, transport=t)
    service = PersistentLlmService(
        client,
        usage_evaluator=lambda raw: UsageEvidence(10, "USD", "test-v1", raw["id"]),
    )
    with Session(c.engine) as db:
        binding = open_pinned_dialog(
            db,
            c.scope,
            {
                "instructions": "고정 지침",
                "ai_config": {
                    "llm_provider": CONFIG.provider,
                    "llm_model": CONFIG.model,
                    "llm_base_url": CONFIG.base_url,
                },
            },
        )
        account = LlmBudgetAccount(
            scope_kind="experiment",
            scope_key=str(c.org_id),
            currency="USD",
            tariff_version="test-v1",
            limit_amount=1000,
            limit_count=10,
        )
        db.add(account)
        db.commit()
        binding_id = binding.id
        account_id = account.id
    return c, t, service, binding_id, account_id


def admit(protocol, db, key="client-1", question="질문", references=None):
    c, t, service, binding_id, account_id = protocol
    references = (
        references
        if references is not None
        else {"references": ["당회 비공개 검색자료"]}
    )
    payload = service.materialize_turn(
        db, c.scope, question, turn_context=references, output_tokens=20
    )
    quote = BudgetQuote(
        (account_id,), "USD", "test-v1", 20, 100000, 20, payload_hash(payload)
    )
    return service.admit_turn(
        db, c.scope, key, question, turn_context=references, quote=quote
    )["event_id"]


def confirm(protocol, db, event_id):
    c, t, service, _, _ = protocol
    view = service.view_turn(db, c.scope, event_id)
    service.ack_display(
        db,
        c.scope,
        event_id,
        message_id=view["message"]["id"],
        content_hash=view["message"]["hash"],
    )
    return service.sync_turn(db, c.scope, event_id)


def test_next_input_is_exact_committed_transcript(protocol):
    c, t, s, binding_id, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        view = s.process_turn(db, c.scope, event)
        assert view["phase"] == "pending_delivery" and t.items == []
        assert confirm(protocol, db, event)["phase"] == "committed"
        second = admit(
            protocol, db, "client-2", "후속 질문", {"references": ["두번째 당회자료"]}
        )
        s.process_turn(db, c.scope, second)
        body = [
            v[2] for v in t.calls if v[0] == "POST" and v[1].endswith("/responses")
        ][-1]
        assert body["input"][:2] == [
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "보여줄 답변", "phase": "final_answer"},
        ]
        assert "당회 비공개 검색자료" not in json.dumps(body, ensure_ascii=False)
        assert (
            len(t.items) == 2
            and db.get(ChannelLlmBinding, binding_id).committed_turn_count == 1
        )


def test_invalid_json_creates_no_canonical_answer(protocol):
    c, t, s, _, account_id = protocol
    t.response_mode = "invalid"
    with Session(c.engine) as db:
        event = admit(protocol, db)
        view = s.process_turn(db, c.scope, event)
        assert (
            view["phase"] == "invalid_response"
            and view["message"] is None
            and t.items == []
        )
        assert db.get(LlmBudgetAccount, account_id).spent_amount == 10
        assert not list(
            db.scalars(
                select(ChannelMessage).where(
                    ChannelMessage.room_id == c.room_id,
                    ChannelMessage.participant_kind == "bot",
                )
            )
        )
        with pytest.raises(TurnConflict):
            admit(protocol, db, "client-2")


def test_display_transform_uses_server_text(protocol):
    c, t, s, _, _ = protocol
    s.display_transform = lambda text: "최종 Talk: " + text
    with Session(c.engine) as db:
        event = admit(protocol, db)
        view = s.process_turn(db, c.scope, event)
        assert view["message"]["text"] == "최종 Talk: 보여줄 답변"
        confirm(protocol, db, event)
        assert t.items[1]["content"][0]["text"] == view["message"]["text"]


def test_forged_or_duplicate_ack_cannot_change_memory(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        view = s.process_turn(db, c.scope, event)
        with pytest.raises(TurnConflict):
            s.ack_display(
                db,
                replace(c.scope, principal_key="user:" + str(uuid4())),
                event,
                message_id=view["message"]["id"],
                content_hash=view["message"]["hash"],
            )
        db.rollback()
        with pytest.raises(TurnConflict):
            s.ack_display(
                db,
                c.scope,
                event,
                message_id=view["message"]["id"],
                content_hash="0" * 64,
            )
        db.rollback()
        confirm(protocol, db, event)
        confirm(protocol, db, event)
        assert t.appends == 1 and len(t.items) == 2


def test_display_failure_redelivers_without_generation(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        first = s.process_turn(db, c.scope, event)
        second = s.process_turn(db, c.scope, event)
        assert first == second and t.generations == 1 and t.items == []
        with pytest.raises(TurnConflict):
            admit(protocol, db, "client-2")


def test_partial_item_append_blocks_next_turn(protocol):
    c, t, s, _, _ = protocol
    t.append_mode = "partial"
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        assert confirm(protocol, db, event)["phase"] == "needs_reconcile"
        assert (
            s.sync_turn(db, c.scope, event)["phase"] == "needs_reconcile"
            and t.appends == 1
        )
        with pytest.raises(TurnConflict):
            admit(protocol, db, "client-2")


def test_ambiguous_append_is_not_retried(protocol):
    c, t, s, _, _ = protocol
    t.append_mode = "timeout_before"
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        assert confirm(protocol, db, event)["phase"] == "needs_reconcile"
        s.sync_turn(db, c.scope, event)
        assert t.appends == 1 and t.generations == 1 and t.items == []


def test_append_lost_receipt_reconciles_by_exact_prefix_without_repost(protocol):
    c, t, s, _, _ = protocol
    t.append_mode = "timeout_after"
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        assert confirm(protocol, db, event)["phase"] == "needs_reconcile"
        assert s.sync_turn(db, c.scope, event)["phase"] == "committed"
        assert t.appends == 1 and t.generations == 1 and len(t.items) == 2


def test_response_completed_but_db_failed_does_not_generate_again(
    protocol, monkeypatch
):
    c, t, s, _, account_id = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic DB commit failure")

        monkeypatch.setattr(s, "_persist_response", fail)
        assert s.process_turn(db, c.scope, event)["phase"] == "unknown"
        s.process_turn(db, c.scope, event)
        assert (
            t.generations == 1
            and db.get(LlmBudgetAccount, account_id).reserved_amount == 20
        )


def test_cancel_unknown_keeps_reservation(protocol):
    c, t, s, _, account_id = protocol
    t.response_mode = "background"
    t.cancel_unknown = True
    with Session(c.engine) as db:
        event = admit(protocol, db)
        assert s.process_turn(db, c.scope, event)["phase"] == "response_pending"
        assert s.cancel_turn(db, c.scope, event)["phase"] == "unknown"
        a = db.get(LlmBudgetAccount, account_id)
        assert (a.reserved_amount, a.reserved_count, a.spent_count) == (20, 1, 0)
        assert t.generations == 1


def test_duplicate_message_key_is_server_idempotent(protocol):
    c, t, s, _, account_id = protocol
    with Session(c.engine) as db:
        first = admit(protocol, db)
        assert admit(protocol, db) == first
        with pytest.raises(TurnConflict):
            admit(protocol, db, question="바뀐 질문")
        db.rollback()
        assert db.get(LlmBudgetAccount, account_id).reserved_count == 1


def test_late_worker_error_cannot_replace_committed_result(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        stale_fence = db.get(ChannelQueueEvent, UUID(event)).lease_fence
        confirm(protocol, db, event)
        assert s._unknown(db, c.scope, event, fence=stale_fence)["phase"] == "committed"
        assert t.generations == 1 and t.appends == 1


def test_cancel_before_paid_submit_releases_reserved_budget_and_fences_worker(protocol):
    c, t, s, _, account_id = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        row = db.get(ChannelQueueEvent, UUID(event))
        row.phase = "history_check"
        row.lease_fence = 10
        db.commit()
        assert s.cancel_turn(db, c.scope, event)["phase"] == "cancelled"
        assert db.get(ChannelQueueEvent, UUID(event)).lease_fence > 10
        account = db.get(LlmBudgetAccount, account_id)
        assert account.reserved_amount == 0 and account.reserved_count == 0
        assert t.calls == []


def test_duplicate_message_after_commit_uses_original_event_without_new_quote(protocol):
    c, t, s, _, account_id = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        confirm(protocol, db, event)
        assert admit(protocol, db) == event
        assert db.get(LlmBudgetAccount, account_id).spent_count == 1
        assert t.generations == 1 and t.appends == 1


def test_materialized_payload_must_still_match_reserved_hash_at_submit(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        row = db.get(ChannelQueueEvent, UUID(event))
        altered = deepcopy(row.parameter_json)
        altered["payload"]["input"][-1]["content"] = "견적 뒤 바뀐 입력"
        row.parameter_json = altered
        db.commit()
        assert s.process_turn(db, c.scope, event)["phase"] in {
            "unknown",
            "needs_reconcile",
        }
        assert t.generations == 0


def test_provider_append_survives_pg_commit_failure_without_repost(
    protocol, monkeypatch
):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        real_commit = db.commit
        fail_once = [True]

        def flaky_commit():
            row = db.get(ChannelQueueEvent, UUID(event))
            if fail_once[0] and row.phase == "committed":
                fail_once[0] = False
                raise RuntimeError("synthetic canonical commit lost")
            return real_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        assert confirm(protocol, db, event)["phase"] == "needs_reconcile"
        assert len(t.items) == 2 and t.appends == 1
        assert s.sync_turn(db, c.scope, event)["phase"] == "committed"
        assert t.appends == 1 and t.generations == 1


def test_restart_restores_pending_delivery_without_new_generation(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        first = s.process_turn(db, c.scope, event)
    restarted = PersistentLlmService(
        OpenAIConversations(CONFIG, transport=t), usage_evaluator=s.usage_evaluator
    )
    with Session(c.engine) as db:
        restored = restarted.process_turn(db, c.scope, event)
        assert restored == first and t.generations == 1
        restarted.ack_display(
            db,
            c.scope,
            event,
            message_id=restored["message"]["id"],
            content_hash=restored["message"]["hash"],
        )
        assert restarted.sync_turn(db, c.scope, event)["phase"] == "committed"
        assert t.generations == 1 and t.appends == 1


def test_background_recovery_uses_get_without_regeneration(protocol):
    c, t, s, _, _ = protocol
    t.response_mode = "background"
    with Session(c.engine) as db:
        event = admit(protocol, db)
        assert s.process_turn(db, c.scope, event)["phase"] == "response_pending"
    with Session(c.engine) as db:
        assert s.reconcile_turn(db, c.scope, event)["phase"] == "pending_delivery"
        assert t.generations == 1
        confirm(protocol, db, event)
        assert t.appends == 1


def test_unknown_conversation_creation_is_not_automatically_repeated(protocol):
    c, t, s, _, _ = protocol
    t.create_unknown = True
    with Session(c.engine) as db:
        event = admit(protocol, db)
        assert s.process_turn(db, c.scope, event)["phase"] == "unknown"
        s.process_turn(db, c.scope, event)
        s.reconcile_turn(db, c.scope, event)
        assert (
            len(
                [
                    v
                    for v in t.calls
                    if v[0] == "POST" and v[1].endswith("/conversations")
                ]
            )
            == 1
        )
        assert t.generations == 0


def test_render_transform_failure_uses_saved_result_after_repair(protocol):
    c, t, s, _, _ = protocol

    def fail(text):
        raise ValueError("synthetic render transformation failure")

    s.display_transform = fail
    with Session(c.engine) as db:
        event = admit(protocol, db)
        assert s.process_turn(db, c.scope, event)["phase"] == "validated"
        assert t.items == [] and t.generations == 1
        s.display_transform = lambda text: "표출: " + text
        view = s.prepare_display(db, c.scope, event)
        assert (
            view["phase"] == "pending_delivery"
            and view["message"]["text"] == "표출: 보여줄 답변"
        )
        confirm(protocol, db, event)
        assert (
            t.items[1]["content"][0]["text"] == view["message"]["text"]
            and t.generations == 1
        )
