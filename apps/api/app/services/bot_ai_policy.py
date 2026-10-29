from __future__ import annotations

from typing import Any

from app.services.vector_search import intent_vector_config
from app.services.secondary_llm import outcome_mapping


SUPPORTED_NLU_TYPE = "ml"
SUPPORTED_NLU_MODEL = "deep_learning_lite"
SUPPORTED_ANSWER_MODE = "fixed"
RUNTIME_SUPPORTED_ANSWER_MODES = {"fixed", "semantic_rag", "llm_rag", "llm"}
SEMANTIC_NLU_TYPES = {"semantic", "semantic_vector", "semantic_external"}
UNSUPPORTED_ANSWER_MODE_REASON = (
    "선택한 답변 방식은 아직 실행 엔진에 연결되지 않았습니다. "
    "현재 실행 가능한 답변 방식은 정해진 답변, Semantic RAG 답변, LLM RAG 답변입니다."
)


def runtime_block_reason(data_json: dict[str, Any] | None) -> str | None:
    data = data_json if isinstance(data_json, dict) else {}
    nlu_type = str(data.get("nlu_type") or SUPPORTED_NLU_TYPE)
    nlu_model = str(data.get("nlu_model") or data.get("nlu_engine") or SUPPORTED_NLU_MODEL)
    answer_mode = str(data.get("answer_mode") or SUPPORTED_ANSWER_MODE)
    if answer_mode not in RUNTIME_SUPPORTED_ANSWER_MODES:
        return UNSUPPORTED_ANSWER_MODE_REASON
    if nlu_type == SUPPORTED_NLU_TYPE and answer_mode != SUPPORTED_ANSWER_MODE:
        return UNSUPPORTED_ANSWER_MODE_REASON
    if nlu_type in SEMANTIC_NLU_TYPES and answer_mode in {"llm", "llm_rag"}:
        return UNSUPPORTED_ANSWER_MODE_REASON
    if nlu_type == "llm" and answer_mode == "semantic_rag":
        return UNSUPPORTED_ANSWER_MODE_REASON
    if nlu_type in SEMANTIC_NLU_TYPES:
        config = intent_vector_config(data)
        if not config.is_ready:
            missing = ", ".join(config.missing_fields)
            return f"Semantic NLU 실행에는 Intent Vector DB 연결 설정이 필요합니다. 누락: {missing}"
        return None
    if nlu_type == "llm":
        return None
    if nlu_type != SUPPORTED_NLU_TYPE or nlu_model != SUPPORTED_NLU_MODEL:
        return "선택한 NLU 엔진은 아직 실행 엔진에 연결되지 않았습니다. 현재 실행 가능한 조합은 ML + DeepLearning Lite입니다."
    return None


def training_block_reason(data_json: dict[str, Any] | None) -> str | None:
    data = data_json if isinstance(data_json, dict) else {}
    nlu_type = str(data.get("nlu_type") or SUPPORTED_NLU_TYPE)
    nlu_model = str(data.get("nlu_model") or data.get("nlu_engine") or SUPPORTED_NLU_MODEL)
    if nlu_type in SEMANTIC_NLU_TYPES:
        config = intent_vector_config(data)
        if not config.is_ready:
            missing = ", ".join(config.missing_fields)
            return f"Semantic NLU 학습에는 Intent Vector DB 연결 설정이 필요합니다. 누락: {missing}"
        return None
    if nlu_type == "llm":
        return None
    if nlu_type != SUPPORTED_NLU_TYPE or nlu_model != SUPPORTED_NLU_MODEL:
        return "선택한 NLU 엔진은 아직 학습 엔진에 연결되지 않았습니다. 현재 학습 가능한 조합은 ML + DeepLearning Lite입니다."
    return None


def secondary_llm_block_reason(document: dict[str, Any] | None, ai_config: dict[str, Any] | None = None, *, bot_kind: str = "bot") -> str | None:
    """초안은 그대로 저장하며 운영 진입 때만 제작자가 지정한 같은 버전 모듈을 검사한다."""
    if bot_kind == "hub":
        return None
    document = document if isinstance(document, dict) else {}
    system = document.get("system_config")
    config = ai_config if isinstance(ai_config, dict) else (system.get("ai_config", {}) if isinstance(system, dict) else {})
    config = config if isinstance(config, dict) else {}
    if config.get("nlu_type", "ml") not in {"ml", *SEMANTIC_NLU_TYPES}:
        return "1차 의도 인식에는 ML 또는 시멘틱 엔진을 사용해야 합니다."
    secondary = config.get("secondary_llm")
    identifier = secondary.get("module_id") if isinstance(secondary, dict) else None
    if not isinstance(identifier, str) or not identifier.strip():
        return "운영 전에 제작자가 작성한 2차 LLM 답변 모듈을 지정해주세요."
    dialogs = document.get("dialogs")
    modules = [row for row in dialogs if isinstance(row, dict)] if isinstance(dialogs, list) else []
    module = next((row for row in modules if row.get("id") == identifier and row.get("dialogType") in (0, "0") and not row.get("deleted_at") and not row.get("deletedAt") and not row.get("deleted")), None)
    if module is None:
        return "2차 LLM 답변 모듈이 현재 버전에 없습니다. 삭제된 모듈이나 다른 버전의 모듈은 사용할 수 없습니다."
    graphs = document.get("dialog_flow_graphs")
    graphs = graphs if isinstance(graphs, list) else []
    if not any(isinstance(graph, dict) and graph.get("dialogId") == identifier and isinstance(graph.get("nodes"), list) and graph["nodes"] for graph in graphs):
        return "2차 LLM 답변 모듈의 시나리오를 작성해주세요."
    if outcome_mapping(secondary) is None:
        return "2차 LLM의 판정 변수와 서로 다른 답변 성공값·미인식값을 지정해주세요."
    return None
