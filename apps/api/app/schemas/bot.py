from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator
from typing import Literal


NluType = Literal["ml", "semantic", "semantic_vector", "semantic_external", "llm"]
NluModel = Literal[
    "deep_learning_lite",
    "ml_tfidf_linear",
    "ml_keyword_baseline",
    "semantic_engine_default",
    "semantic_embedding_mini",
    "semantic_embedding_large",
    "semantic_ollama_bge_m3",
    "semantic_ollama_all_minilm_l12_v2",
    "semantic_ollama_nomic_embed_text",
    "semantic_ollama_paraphrase_multilingual",
    "semantic_ollama_yxchia_paraphrase_multilingual_minilm_l12_v2_q4",
    "semantic_ollama_qwen3_embedding_4b",
    "semantic_ollama_qwen3_embedding_8b",
    "llm_engine_default",
    "llm_intent_fast",
    "llm_intent_reasoning",
]
AnswerMode = Literal["fixed", "semantic_rag", "llm_rag", "llm"]
LlmProvider = Literal["gemini", "chatgpt", "claude", "groq", "cerebras", "mistral", "ollama", "openrouter"]
LlmModel = Literal[
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gpt-4o-mini",
    "gpt-4o",
    "claude-3-5-haiku",
    "claude-3-5-sonnet",
    "llama-3.3-70b-versatile",
    "mixtral-8x7b",
    "llama3.1-8b",
    "qwen-3-32b",
    "mistral-small",
    "mistral-medium",
    "mistral-large",
    "ollama-local",
    "llama3.1-local",
    "openrouter-auto",
    "openrouter-gpt-4o-mini",
]

DEFAULT_MODEL_BY_NLU_TYPE: dict[str, str] = {
    "ml": "deep_learning_lite",
    "semantic": "semantic_engine_default",
    "semantic_vector": "semantic_engine_default",
    "semantic_external": "semantic_embedding_mini",
    "llm": "llm_engine_default",
}

MODELS_BY_NLU_TYPE: dict[str, set[str]] = {
    "ml": {"deep_learning_lite", "ml_tfidf_linear", "ml_keyword_baseline"},
    "semantic": {
        "semantic_engine_default",
        "semantic_embedding_mini",
        "semantic_embedding_large",
        "semantic_ollama_bge_m3",
        "semantic_ollama_all_minilm_l12_v2",
        "semantic_ollama_nomic_embed_text",
        "semantic_ollama_paraphrase_multilingual",
        "semantic_ollama_yxchia_paraphrase_multilingual_minilm_l12_v2_q4",
        "semantic_ollama_qwen3_embedding_4b",
        "semantic_ollama_qwen3_embedding_8b",
    },
    "semantic_vector": {
        "semantic_engine_default",
    },
    "semantic_external": {
        "semantic_embedding_mini",
        "semantic_embedding_large",
        "semantic_ollama_bge_m3",
        "semantic_ollama_all_minilm_l12_v2",
        "semantic_ollama_nomic_embed_text",
        "semantic_ollama_paraphrase_multilingual",
        "semantic_ollama_yxchia_paraphrase_multilingual_minilm_l12_v2_q4",
        "semantic_ollama_qwen3_embedding_4b",
        "semantic_ollama_qwen3_embedding_8b",
    },
    "llm": {"llm_engine_default", "llm_intent_fast", "llm_intent_reasoning"},
}

MODELS_BY_LLM_PROVIDER: dict[str, set[str]] = {
    "gemini": {"gemini-1.5-flash", "gemini-1.5-pro"},
    "chatgpt": {"gpt-4o-mini", "gpt-4o"},
    "claude": {"claude-3-5-haiku", "claude-3-5-sonnet"},
    "groq": {"llama-3.3-70b-versatile", "mixtral-8x7b"},
    "cerebras": {"llama3.1-8b", "qwen-3-32b"},
    "mistral": {"mistral-small", "mistral-medium", "mistral-large"},
    "ollama": {"ollama-local", "llama3.1-local"},
    "openrouter": {"openrouter-auto", "openrouter-gpt-4o-mini"},
}


class SecondaryLlmConfig(BaseModel):
    module_id: str = Field(default="", max_length=150)


class BotNluAnswerConfigMixin(BaseModel):
    nlu_type: NluType | None = None
    nlu_model: NluModel | None = None
    nlu_engine: NluModel | None = None
    answer_mode: AnswerMode | None = None
    llm_provider: LlmProvider | None = None
    llm_model: LlmModel | None = None
    llm_base_url: str | None = Field(default=None, max_length=1000)
    vector_connections: dict[str, Any] | None = None
    configuration_scoring: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_nlu_model_matches_type(self):
        nlu_type = self.nlu_type or "ml"
        allowed_models = MODELS_BY_NLU_TYPE[nlu_type]
        for field_name in ("nlu_model", "nlu_engine"):
            value = getattr(self, field_name, None)
            if value is not None and value not in allowed_models:
                allowed = ", ".join(sorted(allowed_models))
                raise ValueError(f"{field_name} must be one of {allowed} when nlu_type is {nlu_type}")
        if self.llm_provider and self.llm_model:
            allowed_llm_models = MODELS_BY_LLM_PROVIDER[self.llm_provider]
            if self.llm_model not in allowed_llm_models:
                allowed = ", ".join(sorted(allowed_llm_models))
                raise ValueError(f"llm_model must be one of {allowed} when llm_provider is {self.llm_provider}")
        return self


BotLanguage = Literal["ko", "en", "zh-CN", "ja", "vi", "fr", "de"]


class BotCreateRequest(BotNluAnswerConfigMixin):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    bot_kind: Literal["bot", "hub"] = "bot"
    bot_mode: Literal["text", "voice"] = "text"
    profile_key: Literal["gray", "accent", "outline"] = "accent"
    profile_image_data: str | None = Field(default=None, max_length=4_000_000)
    language: BotLanguage = "ko"
    nlu_engine: NluModel | None = "deep_learning_lite"
    nlu_type: NluType | None = "ml"
    nlu_model: NluModel | None = "deep_learning_lite"
    answer_mode: AnswerMode | None = "fixed"
    llm_provider: LlmProvider | None = "chatgpt"
    llm_model: LlmModel | None = "gpt-4o-mini"
    introduction: str | None = Field(default=None, max_length=2000)
    hub_call_method: Literal["button", "natural"] = "button"


class BotUpdateRequest(BotNluAnswerConfigMixin):
    secondary_llm: SecondaryLlmConfig | None = None
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, min_length=1, max_length=20)
    bot_kind: Literal["bot", "hub"] | None = None
    bot_mode: Literal["text", "voice"] | None = None
    profile_key: Literal["gray", "accent", "outline"] | None = None
    profile_image_data: str | None = Field(default=None, max_length=4_000_000)
    language: BotLanguage | None = None
    introduction: str | None = Field(default=None, max_length=2000)
    settings_scope: str | None = Field(default=None, min_length=1, max_length=120)
    settings_json: dict[str, Any] | None = None


class VersionCreateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    comment: str | None = Field(default=None, max_length=2000)


class VersionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, min_length=1, max_length=20)
    comment: str | None = Field(default=None, max_length=2000)
    version_json: dict[str, Any] | None = None


class VersionDocumentItemsUpdateRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class VersionDialogFlowUpdateRequest(BaseModel):
    dialog: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] = Field(default_factory=dict)


class VersionConfigureUpdateRequest(BaseModel):
    dialogs: list[dict[str, Any]] = Field(default_factory=list)
    dialog_flow_graphs: list[dict[str, Any]] = Field(default_factory=list)


class VersionRetrainingUpdateRequest(BaseModel):
    dialogs: list[dict[str, Any]] = Field(default_factory=list)
    system_config: dict[str, Any] = Field(default_factory=dict)


class AnswerTrainingSource(BaseModel):
    source_type: Literal["text", "pdf"] = "text"
    title: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=10000000)
    file_name: str | None = Field(default=None, max_length=500)
    mime_type: str | None = Field(default=None, max_length=120)
    file_base64: str | None = Field(default=None, max_length=10000000)
    embedding_provider: str | None = Field(default=None, max_length=80)
    embedding_model: str | None = Field(default=None, max_length=200)


class VersionNluTrainRequest(BaseModel):
    answer_training: AnswerTrainingSource | None = None


class RagAnswerConfigureRequest(BaseModel):
    answer_training: AnswerTrainingSource
    target_count: int = Field(default=50, ge=1, le=100)
    target_count_policy: Literal["minimize", "near", "exact"] = "near"


class LlmIntentTestRequest(BaseModel):
    utterance: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=10)


class LlmIntentConfigureRequest(BaseModel):
    utterances: list[str] = Field(min_length=1, max_length=500)
    target_count: int = Field(default=50, ge=1, le=100)
    target_count_policy: Literal["minimize", "near", "exact"] = "near"
    dictionary_terms: list[dict[str, Any]] = Field(default_factory=list)
    entity_terms: list[dict[str, Any]] = Field(default_factory=list)
    llm_provider: str | None = Field(default=None, max_length=80)
    llm_model: str | None = Field(default=None, max_length=160)
    llm_base_url: str | None = Field(default=None, max_length=500)


class SemanticIntentConfigureRequest(BaseModel):
    utterances: list[str] = Field(min_length=1, max_length=500)
    target_count: int = Field(default=50, ge=1, le=100)
    target_count_policy: Literal["minimize", "near", "exact"] = "near"
    dictionary_terms: list[dict[str, Any]] = Field(default_factory=list)
    entity_terms: list[dict[str, Any]] = Field(default_factory=list)
    scoring: dict[str, float] = Field(default_factory=dict)


class MlIntentTokenizeRequest(BaseModel):
    utterances: list[str] = Field(min_length=1, max_length=500)


class MlSeedIntent(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    representative_utterances: list[str] = Field(default_factory=list, max_length=20)


class MlIntentConfigureRequest(BaseModel):
    utterances: list[str] = Field(min_length=1, max_length=500)
    target_count: int = Field(default=50, ge=1, le=100)
    target_count_policy: Literal["minimize", "near", "exact"] = "near"
    seed_intents: list[MlSeedIntent] = Field(default_factory=list, max_length=100)
