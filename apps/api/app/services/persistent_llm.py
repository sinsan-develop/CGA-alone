"""기존 방/메시지/큐의 지속 답변 조정. Provider·표출 확정 사이의 불명을 영속 보존한다."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib, json
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import (
    Bot,
    BotVersion,
    User,
    ChannelRoom,
    ChannelMessage,
    ChannelQueueEvent,
    ChannelLlmBinding,
    LlmBudgetReservation,
)
from app.services.openai_conversations import (
    CanonicalItem,
    ProviderProtocolError,
    ProviderTransportError,
    canonical_hash,
    payload_hash,
    build_detached_payload,
    validated_answer,
)
from app.services.llm_budget import (
    reserve_turn_budget,
    mark_turn_submitted,
    hold_unknown_budget,
    settle_turn_budget,
    release_presubmit_budget,
)


class TurnConflict(ValueError):
    pass


def _uuid(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError):
        raise TurnConflict("대화 식별자가 올바르지 않습니다.") from None


def _phase(event, phase, status=None):
    event.phase = phase
    if status:
        event.status = status
    event.status_changed_at = datetime.now(timezone.utc)
    result = dict(event.result_json or {})
    result["phase_history"] = [*(result.get("phase_history") or []), phase][-30:]
    event.result_json = result


def _result(event, **values):
    event.result_json = {**dict(event.result_json or {}), **values}


class PersistentLlmService:
    def __init__(
        self, client, *, usage_evaluator, display_transform=None, access_validator=None
    ):
        self.client = client
        self.usage_evaluator = usage_evaluator
        self.display_transform = display_transform or (lambda text: text)
        self.access_validator = access_validator

    def _binding(self, db, scope, *, read_only=False):
        # lock 순서: 버전 -> 기존 방 -> binding -> event -> 정렬 예산 계정.
        version = db.scalar(
            select(BotVersion)
            .where(BotVersion.id == scope.version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        room = db.scalar(
            select(ChannelRoom)
            .where(ChannelRoom.id == scope.room_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        binding = db.scalar(
            select(ChannelLlmBinding)
            .where(
                ChannelLlmBinding.room_id == scope.room_id,
                ChannelLlmBinding.principal_key == scope.principal_key,
                ChannelLlmBinding.generation == scope.generation,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not str(scope.principal_key).startswith("user:"):
            raise TurnConflict("인증된 주체가 필요합니다.")
        user = db.get(User, _uuid(scope.principal_key[5:]), populate_existing=True)
        bot = db.get(Bot, scope.bot_id, populate_existing=True)
        if (
            version is None
            or room is None
            or binding is None
            or user is None
            or bot is None
            or user.deleted_at is not None
            or user.status != "active"
            or bot.deleted_at is not None
            or user.organization_id != scope.org_id
            or user.group_id != scope.group_id
            or bot.created_by != user.id
            or bot.organization_id != scope.org_id
            or bot.group_id != scope.group_id
            or version.bot_id != scope.bot_id
            or room.bot_id != scope.bot_id
            or (
                room.bot_version_id != scope.version_id
                and not (read_only and binding.status == "closed")
            )
            or binding.organization_id != scope.org_id
            or binding.group_id != scope.group_id
            or binding.bot_id != scope.bot_id
            or binding.version_id != scope.version_id
            or binding.user_id != user.id
            or (
                room.deleted_at is not None
                and not (read_only and binding.status == "closed")
            )
        ):
            raise TurnConflict("대화의 인증 범위가 일치하지 않습니다.")
        if not read_only and (
            binding.status != "open"
            or room.status != "open"
            or version.deleted_at is not None
        ):
            raise TurnConflict("열린 대화만 사용할 수 있습니다.")
        if self.access_validator is not None:
            self.access_validator(db, scope, read_only=read_only)
        return binding, room

    def _event(self, db, scope, event_id, *, read_only=False):
        binding, room = self._binding(db, scope, read_only=read_only)
        event = db.scalar(
            select(ChannelQueueEvent)
            .where(ChannelQueueEvent.id == _uuid(event_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            event is None
            or event.deleted_at is not None
            or event.operation_kind != "persistent_llm"
            or event.binding_id != binding.id
            or event.room_id != room.id
            or event.bot_id != scope.bot_id
            or event.bot_version_id != scope.version_id
        ):
            raise TurnConflict("대화 요청 범위가 일치하지 않습니다.")
        return binding, event

    def _history(self, db, binding):
        messages = db.scalars(
            select(ChannelMessage).where(
                ChannelMessage.room_id == binding.room_id,
                ChannelMessage.deleted_at.is_(None),
            )
        ).all()
        committed = []
        for message in messages:
            receipt = (message.payload_json or {}).get("persistent_llm", {})
            if (
                receipt.get("binding_id") == str(binding.id)
                and receipt.get("canonical") is True
            ):
                committed.append(
                    (
                        receipt.get("order"),
                        CanonicalItem(
                            receipt.get("provider_item_id"),
                            (
                                "user"
                                if message.participant_kind == "user"
                                else "assistant"
                            ),
                            message.text,
                        ),
                    )
                )
        committed.sort(key=lambda value: value[0])
        if [v[0] for v in committed] != list(range(2 * binding.committed_turn_count)):
            raise TurnConflict("확정 대화의 순서를 확인해야 합니다.")
        history = [value[1] for value in committed]
        if (
            binding.memory_prefix_hash
            and canonical_hash(history) != binding.memory_prefix_hash
        ):
            raise TurnConflict("확정 대화 hash가 일치하지 않습니다.")
        return history

    def _check_config(self, binding):
        snapshot = binding.snapshot_json or {}
        ai = snapshot.get("ai_config") or {}
        frozen = snapshot.get("resolved_llm_config") or {
            "provider": ai.get("llm_provider"),
            "model": ai.get("llm_model"),
            "base_url": ai.get("llm_base_url"),
        }
        current = {
            "provider": self.client.config.provider,
            "model": self.client.config.model,
            "base_url": self.client.config.base_url,
        }
        if frozen != current:
            raise TurnConflict("시작 시 고정한 Provider·모델·접속 설정이 필요합니다.")
        ref = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
        if binding.provider_config_ref and binding.provider_config_ref != ref:
            raise TurnConflict("Provider 매핑 범위가 달라졌습니다.")
        return ref

    def materialize_turn(self, db, scope, question, *, turn_context, output_tokens):
        binding, _ = self._binding(db, scope)
        self._check_config(binding)
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > 20000
        ):
            raise TurnConflict("질문이 비어 있거나 입력 상한을 넘었습니다.")
        instructions = binding.snapshot_json.get("instructions")
        modules = binding.snapshot_json.get("module_instructions")
        if isinstance(modules, dict):
            dialog_id = (
                turn_context.get("cga_dialog_id")
                if isinstance(turn_context, dict)
                else None
            )
            if dialog_id not in modules:
                raise TurnConflict("고정 버전에서 허용한 모듈이 필요합니다.")
            instructions = modules[dialog_id]
        payload = build_detached_payload(
            self.client.config,
            self._history(db, binding),
            instructions=instructions,
            question=question,
            turn_context=turn_context,
            output_tokens=output_tokens,
            background=binding.snapshot_json.get("background") is True,
        )
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 262144:
            raise TurnConflict("입력 상한입니다. 새 대화가 필요합니다.")
        return payload

    def admit_turn(self, db, scope, message_key, question, *, turn_context, quote):
        try:
            binding, room = self._binding(db, scope)
            if not isinstance(message_key, str) or not 1 <= len(message_key) <= 120:
                raise TurnConflict("메시지 키가 필요합니다.")
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "question": question,
                        "turn_context": turn_context,
                        "output_tokens": quote.output_tokens_upper,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            existing = db.scalar(
                select(ChannelQueueEvent).where(
                    ChannelQueueEvent.binding_id == binding.id,
                    ChannelQueueEvent.client_message_key == message_key,
                )
            )
            if existing is not None:
                if existing.request_hash != digest:
                    raise TurnConflict("같은 메시지 키의 내용이 달라졌습니다.")
                return self._view(existing)
            pending = db.scalar(
                select(ChannelQueueEvent.id)
                .where(
                    ChannelQueueEvent.binding_id == binding.id,
                    ChannelQueueEvent.operation_kind == "persistent_llm",
                    ChannelQueueEvent.phase != "committed",
                )
                .limit(1)
            )
            if pending is not None or binding.provider_state not in {
                "uninitialized",
                "ready",
            }:
                raise TurnConflict("이전 답변의 확정 또는 복구가 필요합니다.")
            payload = self.materialize_turn(
                db,
                scope,
                question,
                turn_context=turn_context,
                output_tokens=quote.output_tokens_upper,
            )
            if payload_hash(payload) != quote.materialized_input_hash:
                raise TurnConflict("견적과 생성 입력이 다릅니다.")
            binding.provider_config_ref = self._check_config(binding)
            message = ChannelMessage(
                room_id=room.id,
                channel_type=room.channel_type,
                participant_id=str(binding.user_id),
                participant_kind="user",
                participant_name=room.participant_name,
                text=question,
                payload_json={
                    "persistent_llm": {
                        "binding_id": str(binding.id),
                        "generation": binding.generation,
                        "canonical": False,
                    }
                },
            )
            db.add(message)
            db.flush()
            event = ChannelQueueEvent(
                room_id=room.id,
                request_message_id=message.id,
                channel_type=room.channel_type,
                bot_id=scope.bot_id,
                bot_version_id=scope.version_id,
                participant_id=str(binding.user_id),
                operation_kind="persistent_llm",
                binding_id=binding.id,
                client_message_key=message_key,
                request_hash=digest,
                phase="admitted",
                parameter_json={"payload": payload, "question": question},
                result_json={},
            )
            db.add(event)
            db.flush()
            reserve_turn_budget(db, event.id, quote)
            db.commit()
            return self._view(event)
        except Exception:
            db.rollback()
            raise

    def admit_fixed_turn(
        self, db, scope, message_key, question, *, server_text, dialog_id
    ):
        """서버가 기존 시나리오에서 계산한 고정 Talk만 접수한다. 클라이언트 답변은 받지 않는다."""
        try:
            binding, room = self._binding(db, scope)
            if (
                not isinstance(message_key, str)
                or not 1 <= len(message_key) <= 120
                or not isinstance(question, str)
                or not question.strip()
                or not isinstance(server_text, str)
                or not server_text.strip()
            ):
                raise TurnConflict("확정된 질문·서버 Talk·메시지 키가 필요합니다.")
            digest = hashlib.sha256(
                json.dumps(
                    {"question": question, "dialog_id": dialog_id, "kind": "fixed"},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            existing = db.scalar(
                select(ChannelQueueEvent).where(
                    ChannelQueueEvent.binding_id == binding.id,
                    ChannelQueueEvent.client_message_key == message_key,
                )
            )
            if existing is not None:
                if existing.request_hash != digest:
                    raise TurnConflict("같은 메시지 키의 내용이 달라졌습니다.")
                return self._view(existing)
            if db.scalar(
                select(ChannelQueueEvent.id)
                .where(
                    ChannelQueueEvent.binding_id == binding.id,
                    ChannelQueueEvent.operation_kind == "persistent_llm",
                    ChannelQueueEvent.phase != "committed",
                )
                .limit(1)
            ) is not None or binding.provider_state not in {"uninitialized", "ready"}:
                raise TurnConflict("이전 답변의 확정 또는 복구가 필요합니다.")
            binding.provider_config_ref = self._check_config(binding)
            receipt = {
                "persistent_llm": {
                    "binding_id": str(binding.id),
                    "generation": binding.generation,
                    "canonical": False,
                }
            }
            request = ChannelMessage(
                room_id=room.id,
                channel_type=room.channel_type,
                participant_id=str(binding.user_id),
                participant_kind="user",
                participant_name=room.participant_name,
                text=question,
                payload_json=deepcopy(receipt),
            )
            response = ChannelMessage(
                room_id=room.id,
                channel_type=room.channel_type,
                participant_id=str(binding.bot_id),
                participant_kind="bot",
                participant_name="답변 봇",
                text=server_text,
                payload_json=deepcopy(receipt),
            )
            db.add_all([request, response])
            db.flush()
            event = ChannelQueueEvent(
                room_id=room.id,
                request_message_id=request.id,
                channel_type=room.channel_type,
                bot_id=binding.bot_id,
                bot_version_id=binding.version_id,
                participant_id=str(binding.user_id),
                operation_kind="persistent_llm",
                binding_id=binding.id,
                client_message_key=message_key,
                request_hash=digest,
                phase="pending_delivery",
                status="processing",
                parameter_json={
                    "question": question,
                    "cga_dialog_id": dialog_id,
                    "kind": "fixed",
                },
                result_json={},
            )
            db.add(event)
            db.flush()
            content_hash = hashlib.sha256(
                (
                    str(binding.id) + str(event.id) + str(response.id) + server_text
                ).encode()
            ).hexdigest()
            _result(
                event,
                display={
                    "id": str(response.id),
                    "text": server_text,
                    "hash": content_hash,
                },
            )
            _phase(event, "pending_delivery", "processing")
            db.commit()
            return self._view(event)
        except Exception:
            db.rollback()
            raise

    def _view(self, event):
        return {
            "event_id": str(event.id),
            "phase": event.phase,
            "status": event.status,
            "message": deepcopy((event.result_json or {}).get("display")),
            "memory_committed": event.phase == "committed",
        }

    def view_turn(self, db, scope, event_id):
        _, event = self._event(db, scope, event_id, read_only=True)
        return self._view(event)

    def _owned(self, db, scope, event_id, fence):
        binding, event = self._event(db, scope, event_id)
        if event.lease_fence != fence:
            raise TurnConflict("이전 실행의 결과는 적용할 수 없습니다.")
        return binding, event

    def _unknown(self, db, scope, event_id, *, fence, phase="unknown"):
        db.rollback()
        binding, event = self._event(db, scope, event_id)
        if event.lease_fence != fence or event.phase == "committed":
            view = self._view(event)
            db.commit()
            return view
        rows = db.scalars(
            select(LlmBudgetReservation).where(
                LlmBudgetReservation.event_id == event.id
            )
        ).all()
        if (
            event.submitted_at is not None
            and rows
            and all(row.state in {"reserved", "submitted", "unknown"} for row in rows)
        ):
            hold_unknown_budget(db, event.id)
        _phase(event, phase, "unknown")
        event.error_message = "결과 확인이 필요합니다. 자동 재호출하지 않습니다."
        db.commit()
        return self._view(event)

    def process_turn(self, db, scope, event_id):
        binding, event = self._event(db, scope, event_id)
        if event.phase != "reserved":
            return self._view(event)
        self._check_config(binding)
        event.lease_fence += 1
        fence = event.lease_fence
        event.lease_owner = "persistent:" + uuid4().hex
        needs_conversation = binding.provider_conversation_id is None
        _phase(
            event,
            "conversation_creating" if needs_conversation else "history_check",
            "processing",
        )
        if needs_conversation:
            binding.provider_state = "creating"
        binding_id = binding.id
        conversation_id = binding.provider_conversation_id
        db.commit()
        try:
            if needs_conversation:
                conversation_id = self.client.create_conversation(
                    binding_id, request_id="create-" + str(event.id)
                )
                binding, event = self._owned(db, scope, event_id, fence)
                binding.provider_conversation_id = conversation_id
                binding.provider_state = "ready"
                binding.memory_prefix_hash = canonical_hash([])
                _phase(event, "history_check")
                db.commit()
            remote = self.client.list_items(conversation_id)
            binding, event = self._owned(db, scope, event_id, fence)
            if remote != self._history(db, binding):
                db.commit()
                return self._unknown(
                    db, scope, event_id, fence=fence, phase="needs_reconcile"
                )
            payload = deepcopy(event.parameter_json["payload"])
            if payload["input"][:-1] != [
                {
                    "role": i.role,
                    "content": i.text,
                    **({"phase": "final_answer"} if i.role == "assistant" else {}),
                }
                for i in remote
            ]:
                raise TurnConflict("예약 입력과 확정 이력이 달라졌습니다.")
            self._check_config(binding)
            reservations = db.scalars(
                select(LlmBudgetReservation).where(
                    LlmBudgetReservation.event_id == event.id
                )
            ).all()
            if not reservations or any(
                (row.usage_json or {}).get("quote", {}).get("materialized_input_hash")
                != payload_hash(payload)
                or (row.usage_json or {}).get("quote", {}).get("output_tokens_upper")
                != payload.get("max_output_tokens")
                for row in reservations
            ):
                raise TurnConflict("제출 입력이 예약한 입력·출력 상한과 다릅니다.")
            if not mark_turn_submitted(db, event.id):
                return self._view(event)
            db.commit()  # 유료 POST 전 제출 intent와 예약·fence를 영속 저장하고 행 잠금을 놓는다.
            raw = self.client.generate_detached(
                payload, request_id="generate-" + str(event.id)
            )
            self._persist_response(db, scope, event_id, fence, raw)
            return self._apply_response(db, scope, event_id, fence)
        except (ProviderTransportError, ProviderProtocolError, TurnConflict):
            return self._unknown(db, scope, event_id, fence=fence)
        except Exception:
            # 응답 뒤 PG 실패도 다음 process에서 generate를 반복할 근거가 되지 않는다.
            return self._unknown(db, scope, event_id, fence=fence)

    def _persist_response(self, db, scope, event_id, fence, raw):
        _, event = self._owned(db, scope, event_id, fence)
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
            raise ProviderProtocolError("응답 ID가 필요합니다.")
        event.provider_response_id = identifier
        _result(event, provider_receipt=deepcopy(raw))
        _phase(event, "generated")
        db.commit()

    def _apply_response(self, db, scope, event_id, fence):
        _, event = self._owned(db, scope, event_id, fence)
        raw = (event.result_json or {}).get("provider_receipt", {})
        if raw.get("status") in {"queued", "in_progress"}:
            _phase(event, "response_pending", "processing")
            db.commit()
            return self._view(event)
        try:
            usage = raw.get("usage")
            if not isinstance(usage, dict) or any(
                type(usage.get(k)) is not int or usage[k] < 0
                for k in ("input_tokens", "output_tokens")
            ):
                raise ProviderProtocolError("확정 사용량이 필요합니다.")
            settle_turn_budget(db, event.id, self.usage_evaluator(raw))
            db.commit()
        except Exception:
            return self._unknown(db, scope, event_id, fence=fence)
        # 형식 검사는 부작용 없는 계산이다. 정산 commit 이후 쓰기는 반드시 다시 fence를 획득한다.
        answer = None
        invalid = False
        if raw.get("status") not in {"cancelled", "failed"}:
            try:
                answer = validated_answer(raw)
            except ProviderProtocolError:
                invalid = True
        binding, event = self._owned(db, scope, event_id, fence)
        if event.cancel_requested_at is not None or raw.get("status") in {
            "cancelled",
            "failed",
        }:
            _phase(
                event,
                (
                    "cancelled"
                    if event.cancel_requested_at is not None
                    or raw.get("status") == "cancelled"
                    else "failed"
                ),
                "failed",
            )
            db.commit()
            return self._view(event)
        if (event.result_json or {}).get("display"):
            return self._reuse_display(db, binding, event)
        if invalid:
            _phase(event, "invalid_response", "failed")
            db.commit()
            return self._view(event)
        _result(event, validated_answer=answer)
        _phase(event, "validated")
        db.commit()
        return self.prepare_display(db, scope, event_id, fence=fence)

    def _display_message(self, db, binding, event):
        display = (event.result_json or {}).get("display")
        if not isinstance(display, dict):
            raise TurnConflict("저장된 표출 receipt가 필요합니다.")
        message = db.get(
            ChannelMessage, _uuid(display.get("id")), populate_existing=True
        )
        if (
            message is None
            or message.deleted_at is not None
            or message.room_id != binding.room_id
            or message.participant_kind != "bot"
            or message.participant_id != str(binding.bot_id)
            or message.text != display.get("text")
            or (message.payload_json or {}).get("persistent_llm", {}).get("binding_id")
            != str(binding.id)
        ):
            raise TurnConflict("저장된 표출 메시지 범위를 확인해야 합니다.")
        digest = hashlib.sha256(
            (str(binding.id) + str(event.id) + str(message.id) + message.text).encode()
        ).hexdigest()
        if digest != display.get("hash"):
            raise TurnConflict("저장된 표출 hash가 일치하지 않습니다.")
        return message, display

    def _expected_ack(self, binding, display):
        return {
            "id": display["id"],
            "hash": display["hash"],
            "principal": binding.principal_key,
        }

    def _reuse_display(self, db, binding, event):
        _, display = self._display_message(db, binding, event)
        ack = (event.result_json or {}).get("delivery_receipt")
        attempted = (event.result_json or {}).get("append_attempted") is True
        if (ack is not None and ack != self._expected_ack(binding, display)) or (
            attempted and ack is None
        ):
            raise TurnConflict("표출 확인 receipt의 대조가 필요합니다.")
        _phase(
            event,
            (
                "needs_reconcile"
                if attempted
                else "delivery_confirmed" if ack else "pending_delivery"
            ),
            "processing",
        )
        event.error_message = None
        db.commit()
        return self._view(event)

    def prepare_display(self, db, scope, event_id, *, fence=None):
        binding, event = self._event(db, scope, event_id)
        if (
            fence is not None and event.lease_fence != fence
        ) or event.cancel_requested_at is not None:
            return self._view(event)
        if event.phase != "validated":
            return self._view(event)
        if (event.result_json or {}).get("display"):
            return self._reuse_display(db, binding, event)
        try:
            text = self.display_transform(event.result_json["validated_answer"])
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
        except Exception:
            event.error_message = "표출 변환을 확인해야 합니다. 저장된 답변을 재사용하며 재생성하지 않습니다."
            db.commit()
            return self._view(event)
        room = db.get(ChannelRoom, binding.room_id)
        message = ChannelMessage(
            room_id=room.id,
            channel_type=room.channel_type,
            participant_id=str(binding.bot_id),
            participant_kind="bot",
            participant_name="답변 봇",
            text=text,
            payload_json={
                "persistent_llm": {
                    "binding_id": str(binding.id),
                    "generation": binding.generation,
                    "canonical": False,
                }
            },
        )
        db.add(message)
        db.flush()
        digest = hashlib.sha256(
            (str(binding.id) + str(event.id) + str(message.id) + text).encode()
        ).hexdigest()
        _result(event, display={"id": str(message.id), "text": text, "hash": digest})
        _phase(event, "pending_delivery", "processing")
        db.commit()
        return self._view(event)

    def ack_display(self, db, scope, event_id, *, message_id, content_hash):
        binding, event = self._event(db, scope, event_id)
        display = (event.result_json or {}).get("display")
        if (
            not isinstance(display, dict)
            or display["id"] != str(message_id)
            or display["hash"] != content_hash
        ):
            raise TurnConflict("표출 확인이 서버 메시지와 다릅니다.")
        self._display_message(db, binding, event)
        if event.phase in {
            "delivery_confirmed",
            "memory_syncing",
            "needs_reconcile",
            "committed",
        }:
            return self._view(event)
        if event.phase != "pending_delivery":
            raise TurnConflict("표출 확인 단계가 아닙니다.")
        _result(event, delivery_receipt=self._expected_ack(binding, display))
        _phase(event, "delivery_confirmed")
        db.commit()
        return self._view(event)

    def sync_turn(self, db, scope, event_id):
        binding, event = self._event(db, scope, event_id)
        if event.phase == "committed":
            return self._view(event)
        if event.phase not in {
            "delivery_confirmed",
            "memory_syncing",
            "needs_reconcile",
        }:
            return self._view(event)
        display = (event.result_json or {}).get("display")
        if not display or (event.result_json or {}).get(
            "delivery_receipt"
        ) != self._expected_ack(binding, display):
            raise TurnConflict(
                "인증 주체와 서버 메시지에 고정된 표출 확인 receipt가 필요합니다."
            )
        self._display_message(db, binding, event)
        if event.cancel_requested_at is not None and not (event.result_json or {}).get(
            "append_attempted"
        ):
            return self._view(event)
        event.lease_fence += 1
        fence = event.lease_fence
        _phase(event, "memory_syncing", "processing")
        conversation_id = binding.provider_conversation_id
        create_conversation = (
            conversation_id is None and binding.provider_state == "uninitialized"
        )
        if conversation_id is None and not create_conversation:
            _phase(event, "needs_reconcile", "unknown")
            db.commit()
            return self._view(event)
        if create_conversation:
            binding.provider_state = "creating"
        binding_id = binding.id
        history = self._history(db, binding)
        pair = [
            ("user", event.parameter_json["question"]),
            ("assistant", display["text"]),
        ]
        db.commit()
        try:
            if create_conversation:
                conversation_id = self.client.create_conversation(
                    binding_id, request_id="create-" + str(event.id)
                )
                binding, event = self._owned(db, scope, event_id, fence)
                binding.provider_conversation_id = conversation_id
                binding.provider_state = "ready"
                binding.memory_prefix_hash = canonical_hash([])
                db.commit()
            remote = self.client.list_items(conversation_id)
            binding, event = self._owned(db, scope, event_id, fence)
            if (
                len(remote) == len(history)
                and remote == history
                and not event.result_json.get("append_attempted")
                and event.cancel_requested_at is None
            ):
                _result(event, append_attempted=True)
                db.commit()
                self.client.append_pair(
                    conversation_id,
                    pair[0][1],
                    pair[1][1],
                    request_id="append-" + str(event.id),
                )
                remote = self.client.list_items(conversation_id)
                binding, event = self._owned(db, scope, event_id, fence)
            if (
                remote[: len(history)] != history
                or len(remote) != len(history) + 2
                or [(i.role, i.text) for i in remote[len(history) :]] != pair
            ):
                db.commit()
                return self._unknown(
                    db, scope, event_id, fence=fence, phase="needs_reconcile"
                )
            # PG 승인 prefix가 유일하게 일치할 때만 원격 ID와 표출문을 canonical로 확정한다.
            messages = [
                db.get(ChannelMessage, event.request_message_id),
                db.get(ChannelMessage, _uuid(display["id"])),
            ]
            if (
                messages[0] is None
                or messages[1] is None
                or messages[0].text != pair[0][1]
                or messages[1].text != pair[1][1]
            ):
                raise TurnConflict("확정 전 서버 표출문이 달라졌습니다.")
            for offset, (message, remote_item) in enumerate(zip(messages, remote[-2:])):
                message.payload_json = {
                    **dict(message.payload_json or {}),
                    "persistent_llm": {
                        "binding_id": str(binding.id),
                        "generation": binding.generation,
                        "canonical": True,
                        "order": len(history) + offset,
                        "provider_item_id": remote_item.id,
                        "event_id": str(event.id),
                    },
                }
            binding.committed_turn_count += 1
            binding.memory_prefix_hash = canonical_hash(remote)
            _phase(event, "committed", "completed")
            event.error_message = None
            db.commit()
            return self._view(event)
        except Exception:
            return self._unknown(
                db, scope, event_id, fence=fence, phase="needs_reconcile"
            )

    def reconcile_turn(self, db, scope, event_id):
        _, event = self._event(db, scope, event_id)
        if event.phase in {
            "delivery_confirmed",
            "memory_syncing",
            "needs_reconcile",
        } and (event.result_json or {}).get("display"):
            return self.sync_turn(db, scope, event_id)
        if (
            event.phase not in {"unknown", "response_pending", "submitted", "generated"}
            or not event.provider_response_id
        ):
            return self._view(event)
        event.lease_fence += 1
        fence = event.lease_fence
        identifier = event.provider_response_id
        db.commit()
        try:
            raw = self.client.retrieve_response(identifier)
            if raw.get("id") != identifier:
                raise ProviderProtocolError("조회한 응답 ID가 다릅니다.")
            self._persist_response(db, scope, event_id, fence, raw)
            return self._apply_response(db, scope, event_id, fence)
        except Exception:
            return self._unknown(db, scope, event_id, fence=fence)

    def cancel_turn(self, db, scope, event_id):
        _, event = self._event(db, scope, event_id)
        if event.phase == "committed" or event.cancel_requested_at is not None:
            return self._view(event)
        event.cancel_requested_at = datetime.now(timezone.utc)
        event.lease_fence += 1
        fence = event.lease_fence
        if (event.result_json or {}).get("append_attempted"):
            # 취소는 이미 제출한 이력 저장의 불명을 없애지 않는다. 이후에는 GET 대조만 허용한다.
            _phase(event, "needs_reconcile", "unknown")
            db.commit()
            return self._view(event)
        if event.submitted_at is None:
            release_presubmit_budget(db, event.id)
            _phase(event, "cancelled", "failed")
            db.commit()
            return self._view(event)
        raw = (event.result_json or {}).get("provider_receipt", {})
        identifier = event.provider_response_id
        db.commit()
        if not identifier or raw.get("background") is not True:
            return self._unknown(db, scope, event_id, fence=fence)
        try:
            cancelled = self.client.cancel_response(
                identifier, background=True, request_id="cancel-" + str(event.id)
            )
            if cancelled.get("id") != identifier:
                raise ProviderProtocolError("취소 응답 ID가 다릅니다.")
            self._persist_response(db, scope, event_id, fence, cancelled)
            return self._apply_response(db, scope, event_id, fence)
        except Exception:
            return self._unknown(db, scope, event_id, fence=fence)
