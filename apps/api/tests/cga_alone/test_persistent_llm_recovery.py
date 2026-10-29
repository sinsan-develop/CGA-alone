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


def test_cancel_races_validation_using_two_pg_sessions(protocol, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services import persistent_llm as runtime

    c, t, s, _, _ = protocol
    barrier = Barrier(2)
    original = runtime.validated_answer
    with Session(c.engine) as db:
        event = admit(protocol, db)

    def cancel_in_second_session():
        barrier.wait(timeout=10)
        with Session(c.engine) as other:
            s.cancel_turn(other, c.scope, event)
        barrier.wait(timeout=10)

    def validation_boundary(raw):
        barrier.wait(timeout=10)
        barrier.wait(timeout=10)
        return original(raw)

    monkeypatch.setattr(runtime, "validated_answer", validation_boundary)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cancel_in_second_session)
        with Session(c.engine) as db:
            view = s.process_turn(db, c.scope, event)
            future.result(timeout=10)
            assert view["phase"] in {"cancelled", "unknown"} and view["message"] is None
            assert not list(
                db.scalars(
                    select(ChannelMessage).where(
                        ChannelMessage.room_id == c.room_id,
                        ChannelMessage.participant_kind == "bot",
                    )
                )
            )
    assert t.generations == 1 and t.appends == 0


def test_display_commit_then_read_failure_reuses_same_message_receipt(
    protocol, monkeypatch
):
    c, t, s, _, _ = protocol
    original = s._view
    fail_once = [True]

    def failed_read(event):
        if fail_once[0] and event.phase == "pending_delivery":
            fail_once[0] = False
            raise RuntimeError("synthetic read failure after display commit")
        return original(event)

    monkeypatch.setattr(s, "_view", failed_read)
    with Session(c.engine) as db:
        event = admit(protocol, db)
        first = s.process_turn(db, c.scope, event)
        assert first["phase"] == "unknown" and first["message"] is not None
        preserved = deepcopy(first["message"])
    with Session(c.engine) as db:
        restored = s.reconcile_turn(db, c.scope, event)
        assert (
            restored["phase"] == "pending_delivery" and restored["message"] == preserved
        )
        messages = list(
            db.scalars(
                select(ChannelMessage).where(
                    ChannelMessage.room_id == c.room_id,
                    ChannelMessage.participant_kind == "bot",
                )
            )
        )
        assert len(messages) == 1 and str(messages[0].id) == preserved["id"]
        confirm(protocol, db, event)
        assert t.generations == 1 and t.appends == 1


def test_ack_receipt_is_durable_and_bound_to_server_message(protocol):
    c, t, s, _, _ = protocol
    with Session(c.engine) as db:
        event = admit(protocol, db)
        view = s.process_turn(db, c.scope, event)
        s.ack_display(
            db,
            c.scope,
            event,
            message_id=view["message"]["id"],
            content_hash=view["message"]["hash"],
        )
    with Session(c.engine) as db:
        row = db.get(ChannelQueueEvent, UUID(event))
        assert row.result_json["delivery_receipt"] == {
            "id": view["message"]["id"],
            "hash": view["message"]["hash"],
            "principal": c.scope.principal_key,
        }
        assert s.sync_turn(db, c.scope, event)["phase"] == "committed"


@pytest.mark.parametrize("mode", ["timeout_before", "timeout_after"])
def test_cancel_cannot_erase_ambiguous_memory_append(protocol, mode):
    from app.services.version_pins import close_pinned_dialog, PinConflict

    c, t, s, binding_id, _ = protocol
    t.append_mode = mode
    with Session(c.engine) as db:
        event = admit(protocol, db)
        s.process_turn(db, c.scope, event)
        assert confirm(protocol, db, event)["phase"] == "needs_reconcile"
        assert s.cancel_turn(db, c.scope, event)["phase"] == "needs_reconcile"
        reconciled = s.reconcile_turn(db, c.scope, event)
        if mode == "timeout_after":
            assert reconciled["phase"] == "committed"
        else:
            assert reconciled["phase"] == "needs_reconcile"
            with pytest.raises(PinConflict):
                close_pinned_dialog(db, binding_id)
        assert t.appends == 1 and t.generations == 1


# 실제 자식 프로세스를 종료한다. 전송 대역의 원격 상태는 파일로 살아남는다.
PROCESS_WORKER = r"""
import json,os,sys
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from app.schemas.persistent_llm import TurnScope,UsageEvidence
from app.models import ChannelQueueEvent,LlmBudgetReservation
from app.services.openai_conversations import OpenAIConversations
from app.services.persistent_llm import PersistentLlmService
import app.services.persistent_llm as module
from tests.cga_alone.test_persistent_llm_recovery import MemoryTransport
from tests.cga_alone.test_persistent_llm_provider import CONFIG
scope_json,event_key,state_file,operation,crash=sys.argv[1:]
print(json.dumps({'worker_pid':os.getpid(),'operation':operation,'crash':crash}),flush=True)
scope_json=json.loads(scope_json)
scope=TurnScope(**{key:(value if key in {'principal_key','generation'} else UUID(value)) for key,value in scope_json.items()})
url=os.environ['AIDOT_TEST_DATABASE_URL']
assert url.rsplit('/',1)[-1]=='cga_alone_qa'
engine=create_engine(url,pool_pre_ping=True)
state_path=Path(state_file)
class DurableTransport(MemoryTransport):
    def __init__(self):
        super().__init__()
        if state_path.exists():
            saved=json.loads(state_path.read_text(encoding='utf-8'))
            for key in ['items','calls','generations','appends']:
                setattr(self,key,saved[key])
    def __call__(self,method,url,payload,headers,timeout):
        with state_path.with_suffix('.requests').open('a',encoding='utf-8') as journal:
            journal.write(json.dumps({'method':method,'url':url})+'\n')
        if crash=='partial_append': self.append_mode='partial'
        raw=super().__call__(method,url,payload,headers,timeout)
        state_path.write_text(json.dumps({key:getattr(self,key) for key in ['items','calls','generations','appends']},ensure_ascii=False),encoding='utf-8')
        if crash=='remote_completed' and method=='POST' and url.endswith('/responses'):os._exit(77)
        if crash in {'partial_append','full_append'} and method=='POST' and url.endswith('/items'):os._exit(77)
        return raw
class Service(PersistentLlmService):
    def _persist_response(self,*args):
        result=super()._persist_response(*args)
        if crash=='response_id':os._exit(77)
        return result
if operation=='raced_process':
    import time
    (state_path.parent/('worker-ready-'+str(os.getpid()))).touch()
    stop=time.monotonic()+10
    while len(list(state_path.parent.glob('worker-ready-*')))<2:
        if time.monotonic()>stop:raise RuntimeError('QA process barrier timeout')
        time.sleep(.01)
    operation='process'
transport=DurableTransport()
service=Service(OpenAIConversations(CONFIG,transport=transport),usage_evaluator=lambda raw:UsageEvidence(10,'USD','test-v1',raw['id']))
if crash=='before_submit':module.mark_turn_submitted=lambda *args:os._exit(77)
with Session(engine,autoflush=False) as db:
    original_commit=db.commit
    def commit():
        event=db.get(ChannelQueueEvent,UUID(event_key))
        phase=event.phase if event else None
        settled=False
        if crash=='usage_committed' and phase=='generated':
            rows=db.scalars(select(LlmBudgetReservation).where(LlmBudgetReservation.event_id==event.id)).all()
            settled=bool(rows) and all(row.state=='settled' for row in rows)
        original_commit()
        if (crash=='after_submit' and phase=='submitted') or (crash=='usage_committed' and settled) or (crash=='display_committed' and phase=='pending_delivery') or (crash=='ack_committed' and phase=='delivery_confirmed') or (crash=='canonical_committed' and phase=='committed'):
            os._exit(77)
    db.commit=commit
    if operation=='process':result=service.process_turn(db,scope,event_key)
    elif operation=='recover':
        service.process_turn(db,scope,event_key)
        result=service.reconcile_turn(db,scope,event_key)
    elif operation=='ack':
        view=service.view_turn(db,scope,event_key)
        result=service.ack_display(db,scope,event_key,message_id=view['message']['id'],content_hash=view['message']['hash'])
    elif operation=='sync':result=service.sync_turn(db,scope,event_key)
    elif operation=='confirm':
        view=service.view_turn(db,scope,event_key)
        service.ack_display(db,scope,event_key,message_id=view['message']['id'],content_hash=view['message']['hash'])
        result=service.sync_turn(db,scope,event_key)
    else:raise AssertionError('unknown test operation')
    print(json.dumps(result,ensure_ascii=False))
engine.dispose()
"""


def process_worker(c, event_id, state_path, operation, crash="", expected=0):
    """종료 대상은 이번 테스트가 생성한 자식 한 개뿐이다. 인증정보는 argv/로그에 넣지 않는다."""
    from dataclasses import asdict
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            PROCESS_WORKER,
            json.dumps(asdict(c.scope), default=str),
            str(event_id),
            str(state_path),
            operation,
            crash,
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    with state_path.with_suffix(".workers").open("a", encoding="utf-8") as receipt:
        receipt.write(
            json.dumps(
                {
                    "returncode": result.returncode,
                    "operation": operation,
                    "crash": crash,
                    "worker": (
                        json.loads(result.stdout.splitlines()[0])
                        if result.stdout
                        else None
                    ),
                }
            )
            + "\n"
        )
    assert result.returncode == expected, result.stderr[-1800:]
    return json.loads(result.stdout.splitlines()[-1]) if expected == 0 else None


@pytest.mark.parametrize(
    "crash,expected_generations",
    [("before_submit", 0), ("after_submit", 0), ("remote_completed", 1)],
)
def test_real_process_restart_marks_expired_unknown_without_reposting(
    protocol, tmp_path, crash, expected_generations
):
    from datetime import datetime, timedelta, timezone

    c, _, service, _, account_id = protocol
    state = tmp_path / "remote-state.json"
    with Session(c.engine) as db:
        event = admit(protocol, db)
    process_worker(c, event, state, "process", crash, expected=77)
    with Session(c.engine) as db:
        row = db.get(ChannelQueueEvent, UUID(event))
        assert (
            row.lease_expires_at is not None
        ), "중단된 실행을 판정할 영속 deadline이 필요합니다"
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    restored = process_worker(c, event, state, "recover")
    assert restored["phase"] == "unknown" and restored["message"] is None
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["generations"] == expected_generations
    with Session(c.engine) as db:
        account = db.get(LlmBudgetAccount, account_id)
        assert (
            account.reserved_count == 1
            and account.reserved_amount == 20
            and account.spent_count == 0
        )
        with pytest.raises(TurnConflict):
            admit(protocol, db, "next-blocked")
        db.rollback()
        rows = db.scalars(
            select(LlmBudgetReservation).where(
                LlmBudgetReservation.event_id == UUID(event)
            )
        ).all()
        assert all(
            row.state == ("reserved" if crash == "before_submit" else "unknown")
            for row in rows
        )
    preserve_process_evidence(c, event, state, crash)


@pytest.mark.parametrize(
    "crash", ["response_id", "usage_committed", "display_committed"]
)
def test_real_process_recovers_saved_response_without_generation_or_duplicate_display(
    protocol, tmp_path, crash
):
    c, _, _, _, account_id = protocol
    state = tmp_path / "remote-state.json"
    with Session(c.engine) as db:
        event = admit(protocol, db)
    process_worker(c, event, state, "process", crash, expected=77)
    with Session(c.engine) as db:
        original = (db.get(ChannelQueueEvent, UUID(event)).result_json or {}).get(
            "display"
        )
    recovered = process_worker(c, event, state, "recover")
    assert recovered["phase"] == "pending_delivery"
    if original:
        assert recovered["message"] == original
    confirmed = process_worker(c, event, state, "confirm")
    assert (
        confirmed["phase"] == "committed"
        and confirmed["message"]["id"] == recovered["message"]["id"]
    )
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert (
        saved["generations"] == 1 and saved["appends"] == 1 and len(saved["items"]) == 2
    )
    assert "당회 비공개 검색자료" not in json.dumps(saved["items"], ensure_ascii=False)
    with Session(c.engine) as db:
        account = db.get(LlmBudgetAccount, account_id)
        assert (
            account.spent_count == 1
            and account.spent_amount == 10
            and account.reserved_count == 0
        )
        messages = db.scalars(
            select(ChannelMessage).where(
                ChannelMessage.room_id == c.room_id,
                ChannelMessage.participant_kind == "bot",
            )
        ).all()
        assert (
            len(messages) == 1
            and (messages[0].payload_json or {})["persistent_llm"]["canonical"] is True
        )
    preserve_process_evidence(c, event, state, crash)


@pytest.mark.parametrize(
    "crash", ["ack_committed", "partial_append", "full_append", "canonical_committed"]
)
def test_real_process_ack_and_append_crashes_never_repeat_remote_writes(
    protocol, tmp_path, crash
):
    c, _, service, binding_id, account_id = protocol
    state = tmp_path / "remote-state.json"
    with Session(c.engine) as db:
        event = admit(protocol, db)
    first = process_worker(c, event, state, "process")
    process_worker(
        c,
        event,
        state,
        "ack",
        crash if crash == "ack_committed" else "",
        expected=77 if crash == "ack_committed" else 0,
    )
    if crash != "ack_committed":
        process_worker(c, event, state, "sync", crash, expected=77)
    recovered = process_worker(c, event, state, "recover")
    assert recovered["message"]["id"] == first["message"]["id"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["generations"] == 1 and saved["appends"] == 1
    expected = "needs_reconcile" if crash == "partial_append" else "committed"
    assert recovered["phase"] == expected
    with Session(c.engine) as db:
        account = db.get(LlmBudgetAccount, account_id)
        assert account.spent_count == 1 and account.reserved_count == 0
        binding = db.get(ChannelLlmBinding, binding_id)
        assert binding.status == "open" and binding.committed_turn_count == (
            0 if crash == "partial_append" else 1
        )
        if crash == "partial_append":
            with pytest.raises(TurnConflict):
                admit(protocol, db, "next-after-partial")
    preserve_process_evidence(c, event, state, crash)


def test_two_real_workers_claim_one_event_with_one_generation(protocol, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    c, _, _, _, account_id = protocol
    state = tmp_path / "remote-state.json"
    with Session(c.engine) as db:
        event = admit(protocol, db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: process_worker(c, event, state, "raced_process"), range(2)
            )
        )
    assert any(result["phase"] == "pending_delivery" for result in results)
    calls = [
        json.loads(line)
        for line in state.with_suffix(".requests")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert (
        len(
            [
                row
                for row in calls
                if row["method"] == "POST" and row["url"].endswith("/responses")
            ]
        )
        == 1
    )
    with Session(c.engine) as db:
        account = db.get(LlmBudgetAccount, account_id)
        assert account.spent_count == 1 and account.reserved_count == 0
        assert db.get(ChannelQueueEvent, UUID(event)).phase == "pending_delivery"
    preserve_process_evidence(c, event, state, "two-workers")


@pytest.mark.parametrize(
    "database,role,same",
    [
        ("cga_alone", "cga_alone", False),
        ("other_qa", "cga_alone", False),
        ("cga_alone_qa", "postgres", False),
        ("cga_alone_qa", "cga_alone", True),
    ],
)
def test_qa_server_refuses_service_database_or_privileged_role(database, role, same):
    from sqlalchemy.engine import URL
    from tests.cga_alone.persistent_llm_qa_server import validate_qa_url

    candidate = URL.create(
        "postgresql+psycopg",
        username=role,
        password="test-only",
        host="db",
        database=database,
    ).render_as_string(hide_password=False)
    production = (
        candidate
        if same
        else URL.create(
            "postgresql+psycopg", username="cga_alone", host="db", database="cga_alone"
        ).render_as_string(hide_password=False)
    )
    with pytest.raises(ValueError):
        validate_qa_url(candidate, production)


def test_qa_server_restores_same_identity_and_file_provider_then_cleans(
    test_engine, migrated_test_database, tmp_path
):
    from tests.cga_alone.persistent_llm_qa_server import (
        _seed,
        _context,
        _configure,
        _cleanup,
    )
    from app.main import app
    from app.api.routes import bots
    from fastapi.testclient import TestClient
    from tests.integration.test_bot_persistence_regression import _request
    from app.models import User
    from app.core.config import ROOT_DIR

    private = tmp_path / "owner-private.json"
    remote = tmp_path / "provider-state.json"
    c, packet, generators = _seed(test_engine, private)
    try:
        _configure(c, packet, remote)
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"login_id": packet["login_id"], "password": packet["password"]},
            )
            assert login.status_code == 200
            headers = {
                "Authorization": "Bearer " + login.json()["data"]["access_token"]
            }
            base = f"/api/v1/bots/{c.bot_id}/persistent"
            started = client.post(
                base + "/rooms", headers=headers, json={"dialog_id": "pllm-module"}
            )
            assert started.status_code == 200
            room = started.json()["data"]["room"]["id"]
            for index in range(2):
                if index:
                    c = _context(
                        test_engine, json.loads(private.read_text(encoding="utf-8"))
                    )
                    _configure(c, packet, remote)
                sent = client.post(
                    base + f"/rooms/{room}/turns",
                    headers=headers,
                    json={
                        "expected_generation": 1,
                        "message_key": f"qa-server-{index}",
                        "question": f"영속 대역 질문{index}",
                        "dialog_id": "pllm-module",
                    },
                )
                assert sent.status_code == 202
                event = sent.json()["data"]["event_id"]
                with Session(test_engine) as db:
                    bots._process_persistent_llm_events(db, _request(), limit=10)
                view = client.get(
                    base + f"/rooms/{room}/turns/{event}", headers=headers
                ).json()["data"]
                assert view["phase"] == "pending_delivery"
                ack = client.post(
                    base + f"/rooms/{room}/turns/{event}/ack",
                    headers=headers,
                    json={
                        "message_id": view["message"]["id"],
                        "content_hash": view["message"]["hash"],
                    },
                )
                assert ack.status_code == 202
                with Session(test_engine) as db:
                    bots._process_persistent_llm_events(db, _request(), limit=10)
            state = json.loads(remote.read_text(encoding="utf-8"))
            inputs = [
                row[2]["input"]
                for row in state["calls"]
                if row[0] == "POST" and row[1].endswith("/responses")
            ]
            assert [len(body) for body in inputs] == [1, 3] and state[
                "generations"
            ] == 2
            with Session(test_engine) as db:
                binding = db.scalar(
                    select(ChannelLlmBinding).where(
                        ChannelLlmBinding.room_id == UUID(room)
                    )
                )
                assert (
                    binding.committed_turn_count == 2
                    and binding.provider_conversation_id in state["conversations"]
                )
        _cleanup(c, packet)
        with Session(test_engine) as db:
            assert db.get(User, c.user_id) is None
        assert not (
            ROOT_DIR / ".local/pllm-fixture-owners" / (str(c.org_id) + ".json")
        ).exists()
    finally:
        context_generator, http_generator, patch = generators
        for generator in (http_generator, context_generator):
            try:
                next(generator)
            except StopIteration:
                pass
        patch.undo()


def test_qa_server_cleanup_refuses_mismatched_group_receipt(persistent_context):
    from tests.cga_alone.persistent_llm_qa_server import _assert_owned
    from types import SimpleNamespace

    c = persistent_context
    wrong = SimpleNamespace(**{**vars(c), "group_id": uuid4()})
    with Session(c.engine) as db:
        with pytest.raises(ValueError):
            _assert_owned(db, wrong)


def preserve_process_evidence(c, event_id, state_path, label, extra=None):
    from pathlib import Path
    from dataclasses import asdict
    from app.models import ChannelLlmVersionGuard

    state = json.loads(state_path.read_text(encoding="utf-8"))
    with Session(c.engine) as db:
        event = db.get(ChannelQueueEvent, UUID(str(event_id)))
        binding = db.get(ChannelLlmBinding, event.binding_id)
        guard = db.get(ChannelLlmVersionGuard, binding.version_id)
        receipt = {
            "scope": asdict(c.scope),
            "event_id": str(event.id),
            "phase": event.phase,
            "fence": event.lease_fence,
            "binding_status": binding.status,
            "provider_conversation_id": binding.provider_conversation_id,
            "committed_turn_count": binding.committed_turn_count,
            "mutation_status": guard.mutation_status,
            "transport": "file-persistent synthetic only",
            "actual_openai_calls": 0,
            "remote": state,
            "extra": extra,
        }
    worker_path = state_path.with_suffix(".workers")
    receipt["workers"] = (
        [
            json.loads(line)
            for line in worker_path.read_text(encoding="utf-8").splitlines()
        ]
        if worker_path.exists()
        else []
    )
    root = Path(__file__).parents[4] / ".local/pllm-p04-process-evidence"
    root.mkdir(parents=True, exist_ok=True)
    (root / (label + "-" + str(event_id) + ".json")).write_text(
        json.dumps(receipt, default=str, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_native_qa_api_restart_preserves_user_bot_room_and_provider_history(
    test_engine, migrated_test_database, tmp_path
):
    import os, socket, subprocess, sys, time
    from pathlib import Path
    from urllib.request import build_opener, ProxyHandler, Request
    from tests.cga_alone.persistent_llm_qa_server import _context

    host = "127.0.0.2"
    port = 8320
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
    env = {**os.environ, "CGA_PERSISTENT_QA": "1"}
    private = tmp_path / "owner-private.json"
    state = tmp_path / "provider-state.json"
    process = None
    logs = []
    pids = []
    opener = build_opener(ProxyHandler({}))

    def http(path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(
            f"http://{host}:{port}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with opener.open(request, timeout=5) as response:
            return json.loads(response.read())

    def start(mode):
        nonlocal process
        log = (tmp_path / (mode + ".log")).open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.cga_alone.persistent_llm_qa_server",
                mode,
                "--state-dir",
                str(tmp_path),
                "--host",
                host,
                "--port",
                str(port),
                "--keep-on-stop",
            ],
            cwd=Path(__file__).parents[2],
            env=env,
            stdout=log,
            stderr=log,
        )
        pids.append(process.pid)
        limit = time.monotonic() + 25
        while time.monotonic() < limit:
            assert process.poll() is None, "QA API가 준비 전에 종료됐습니다"
            try:
                http("/health/ready")
                return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("QA API 준비 시간 초과")

    def stop():
        nonlocal process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process = None

    def wait_turn(base, event, token, phase):
        limit = time.monotonic() + 20
        while time.monotonic() < limit:
            view = http(base + "/turns/" + event, token=token)["data"]
            if view["phase"] == phase:
                return view
            time.sleep(0.1)
        raise AssertionError("실제 QA worker 상태 전이 시간 초과")

    try:
        start("seed")
        packet = json.loads(private.read_text(encoding="utf-8"))
        token = http(
            "/api/v1/auth/login",
            {"login_id": packet["login_id"], "password": packet["password"]},
        )["data"]["access_token"]
        base = "/api/v1/bots/" + packet["owner"]["bot_id"] + "/persistent/rooms"
        room = http(base, {"dialog_id": "pllm-module"}, token)["data"]["room"]["id"]
        base += "/" + room
        conversation = None
        for index in range(2):
            if index:
                stop()
                start("resume")
                token = http(
                    "/api/v1/auth/login",
                    {"login_id": packet["login_id"], "password": packet["password"]},
                )["data"]["access_token"]
                restored = http(base, token=token)["data"]
                assert (
                    restored["room"]["id"] == room
                    and restored["room"]["generation"] == 1
                    and len(restored["messages"]) == 2
                )
            event = http(
                base + "/turns",
                {
                    "expected_generation": 1,
                    "message_key": f"native-{index}",
                    "question": f"실제 API 재시작 질문{index}",
                    "dialog_id": "pllm-module",
                },
                token,
            )["data"]["event_id"]
            pending = wait_turn(base, event, token, "pending_delivery")
            http(
                base + "/turns/" + event + "/ack",
                {
                    "message_id": pending["message"]["id"],
                    "content_hash": pending["message"]["hash"],
                },
                token,
            )
            wait_turn(base, event, token, "committed")
            with Session(test_engine) as db:
                binding = db.scalar(
                    select(ChannelLlmBinding).where(
                        ChannelLlmBinding.room_id == UUID(room)
                    )
                )
                if conversation:
                    assert binding.provider_conversation_id == conversation
                conversation = binding.provider_conversation_id
        remote = json.loads(state.read_text(encoding="utf-8"))
        inputs = [
            row[2]["input"]
            for row in remote["calls"]
            if row[0] == "POST" and row[1].endswith("/responses")
        ]
        assert (
            remote["generations"] == 2
            and [len(body) for body in inputs] == [1, 3]
            and pids[0] != pids[1]
        )
        preserve_process_evidence(
            _context(test_engine, packet),
            event,
            state,
            "native-api-restart",
            {"api_pids": pids, "same_room": room, "input_counts": [1, 3]},
        )
    finally:
        stop()
        for log in logs:
            log.close()
        if private.exists():
            clean = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.cga_alone.persistent_llm_qa_server",
                    "cleanup",
                    "--state-dir",
                    str(tmp_path),
                ],
                cwd=Path(__file__).parents[2],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
            )
            assert clean.returncode == 0, clean.stderr[-1500:]


def test_qa_server_preserve_mode_never_reports_failed_start_as_success(
    test_engine, migrated_test_database, tmp_path, monkeypatch
):
    import sys, uvicorn
    from tests.cga_alone import persistent_llm_qa_server as qa

    c, packet, generators = qa._seed(test_engine, tmp_path / "owner-private.json")

    class RecordedExit(BaseException):
        def __init__(self, code):
            self.code = code

    def exit_now(code):
        raise RecordedExit(code)

    def failed_start(*args, **kwargs):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setenv("CGA_PERSISTENT_QA", "1")
    monkeypatch.setattr(
        sys, "argv", ["qa", "resume", "--state-dir", str(tmp_path), "--keep-on-stop"]
    )
    monkeypatch.setattr(qa, "create_engine", lambda *args, **kwargs: test_engine)
    monkeypatch.setattr(qa.os, "_exit", exit_now)
    monkeypatch.setattr(uvicorn, "run", failed_start)
    try:
        with pytest.raises(RecordedExit) as result:
            qa.main()
        assert result.value.code != 0
    finally:
        context_generator, http_generator, patch = generators
        for generator in (http_generator, context_generator):
            try:
                next(generator)
            except StopIteration:
                pass
        patch.undo()
