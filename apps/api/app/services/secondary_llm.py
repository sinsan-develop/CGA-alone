"""S04의 결과 판정만 담당한다. Provider/세션/시나리오 엔진을 만들지 않는다."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from app.services.runtime_variables import (
    _path_segments, _split_variable_expression, get_variable, stringify_variable, var_key,
)

@dataclass
class SecondaryOutcome:
    status: Literal["answered", "unrecognized", "pending", "error"]
    outputs: list[dict[str, Any]]
    state: dict[str, Any]


def outcome_mapping(config):
    mapping = config.get("outcome") if isinstance(config, dict) else None
    if not isinstance(mapping, dict):
        return None
    keys = ("variableName", "answeredValue", "unrecognizedValue")
    if any(not isinstance(mapping.get(key), str) or not mapping[key].strip() for key in keys):
        return None
    if mapping["answeredValue"] == mapping["unrecognizedValue"]:
        return None
    return {key: mapping[key].strip() if key == "variableName" else mapping[key] for key in keys}


def evaluate_secondary_outcome(config, state, outputs, *, execution_error=False):
    mapping = outcome_mapping(config)
    if execution_error or mapping is None:
        return SecondaryOutcome("error", outputs, state)
    if state.get("waitingNodeId"):
        return SecondaryOutcome("pending", outputs, state)
    variables = state.get("variables")
    variables = variables if isinstance(variables, dict) else {}
    value = get_variable(variables, mapping["variableName"])
    if value is None or not isinstance(value, (str, int, float, bool)):
        return SecondaryOutcome("error", outputs, state)
    value = stringify_variable(value)
    status = "answered" if value == mapping["answeredValue"] else "unrecognized" if value == mapping["unrecognizedValue"] else "error"
    return SecondaryOutcome(status, outputs, state)


def _clear_outcome_value(variables, name):
    """이전 판정 경로만 비운다. 원래 질문·입력 변수와 같은 응답의 다른 필드는 보존한다."""
    root, path = _split_variable_expression(name)
    segments = _path_segments(path)
    canonical = ".".join([root, *segments])
    for key in {name, name.lstrip("$"), var_key(name), canonical, var_key(canonical)}:
        variables.pop(key, None)
    if not segments:
        return
    for key in (root, var_key(root)):
        if key not in variables or not isinstance(variables[key], (dict, list)):
            continue
        value = deepcopy(variables[key])
        current = value
        try:
            for segment in segments[:-1]:
                current = current[int(segment)] if isinstance(current, list) else current[segment]
            if isinstance(current, dict):
                current.pop(segments[-1], None)
            elif isinstance(current, list):
                current[int(segments[-1])] = None
        except (KeyError, IndexError, ValueError, TypeError):
            pass
        variables[key] = value


def _event_cursor(state):
    events = state.get("runtimeEvents") or []
    if not events or not isinstance(events[-1], dict):
        return None
    return [events[-1].get(key) for key in ("time", "event", "nodeId")]


def _new_execution_error(state, cursor):
    events = state.get("runtimeEvents") or []
    start = 0
    if cursor is not None:
        for index, event in enumerate(events):
            if isinstance(event, dict) and [event.get(key) for key in ("time", "event", "nodeId")] == cursor:
                start = index + 1
    return any(isinstance(event, dict) and event.get("level") == "error" for event in events[start:])


def capture_secondary_terminal_result(state):
    """기존 End 변수 초기화 전에 판정값 하나만 일시 보존한다."""
    scope = state.get("__secondaryLlm")
    mapping = outcome_mapping(scope.get("config")) if isinstance(scope, dict) else None
    if mapping:
        name = mapping["variableName"]
        scope["terminal_variables"] = {name: deepcopy(get_variable(state.get("variables") or {}, name))}


def _finish(config, state, outputs, *, execution_error=False):
    scope = state.get("__secondaryLlm")
    terminal_variables = scope.get("terminal_variables") if isinstance(scope, dict) else None
    decision_state = {**state, "variables": terminal_variables} if state.get("sessionEnded") is True and isinstance(terminal_variables, dict) else state
    result = evaluate_secondary_outcome(config, decision_state, outputs, execution_error=execution_error)
    result.state = state
    if result.status != "pending":
        state.pop("__secondaryLlm", None)
    return result


def run_secondary_module(document, state, config, *, run_module, default_messages, channel_code):
    if state.get("__secondaryLlm") or not isinstance(config, dict) or not isinstance(config.get("module_id"), str) or not config["module_id"].strip() or outcome_mapping(config) is None:
        return SecondaryOutcome("error", [], state)
    frozen = deepcopy(config)
    cursor = _event_cursor(state)
    state["__secondaryLlm"] = {"config": frozen, "event_cursor": cursor}
    variables = dict(state.get("variables") or {})
    _clear_outcome_value(variables, frozen["outcome"]["variableName"])
    state["variables"] = variables
    state["dialogEnded"] = False
    state["sessionEnded"] = False
    try:
        outputs, next_state, ran = run_module(document, state, frozen["module_id"], default_messages, channel_code)
        if ran:
            scope = next_state.get("__secondaryLlm")
            next_state["__secondaryLlm"] = {**(scope if isinstance(scope, dict) else {}), "config": frozen, "event_cursor": cursor}
        return _finish(frozen, next_state, outputs, execution_error=not ran or _new_execution_error(next_state, cursor))
    except Exception:
        return _finish(frozen, state, [], execution_error=True)


def resume_secondary_outcome(state, outputs):
    scope = state.get("__secondaryLlm")
    if not isinstance(scope, dict):
        return SecondaryOutcome("error", outputs, state)
    return _finish(scope.get("config"), state, outputs, execution_error=_new_execution_error(state, scope.get("event_cursor")))
