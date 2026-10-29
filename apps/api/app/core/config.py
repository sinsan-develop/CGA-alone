from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "CGA API"
    log_level: str = "INFO"
    api_slow_request_threshold_ms: float = 1000.0
    db_slow_query_threshold_ms: float = 700.0
    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = False
    cache_default_ttl_seconds: int = 60
    cache_version_section_ttl_seconds: int = 60
    operations_dashboard_cache_ttl_seconds: int = 15
    admin_log_scan_max_bytes: int = 8 * 1024 * 1024
    admin_log_scan_max_lines: int = 20_000
    admin_jsonl_scan_max_bytes_per_file: int = 4 * 1024 * 1024
    admin_jsonl_scan_max_entries: int = 10_000
    api_port: int = 8320
    next_public_api_base_url: str = ""
    cors_origins: str = "http://localhost:3320,http://localhost:3330,http://127.0.0.1:3330,http://localhost:5173,http://127.0.0.1:5173"
    cors_origin_regex: str = r"https?://(localhost|127\.0\.0\.1):\d+"
    login_history_retention_days: int = 90
    webchat_api_key: str = ""
    # 지속 LLM 첫 샘플은 모델/횟수/예산 확인 전 실제 실행을 허용하지 않는다.
    persistent_llm_enabled: bool = False
    persistent_llm_execution_enabled: bool = False
    persistent_llm_sample_bot_id: str = ""
    persistent_llm_budget_scope: str = ""
    persistent_llm_tariff_model: str = ""
    persistent_llm_tariff_version: str = ""
    persistent_llm_currency: str = ""
    persistent_llm_context_input_tokens: int = 0
    persistent_llm_max_output_tokens: int = 0
    persistent_llm_budget_amount: int = 0
    persistent_llm_generation_limit: int = 0
    persistent_llm_input_rate_units_per_million: int | None = None
    persistent_llm_output_rate_units_per_million: int | None = None
    persistent_llm_background: bool = False
    channel_queue_worker_enabled: bool = True
    channel_queue_worker_interval_seconds: float = 2.0
    channel_queue_worker_batch_size: int = 10
    channel_queue_worker_error_backoff_seconds: float = 30.0
    edit_lock_cleanup_enabled: bool = True
    edit_lock_cleanup_interval_seconds: float = 60.0
    nlu_training_worker_enabled: bool = True
    nlu_training_worker_interval_seconds: float = 1.0
    nlu_training_worker_error_backoff_seconds: float = 30.0
    cga_license_public_key: str = ""
    cga_legacy_asset_root: str = ""

    openai_api_key: str = ""
    groq_api_key: str = ""
    mistral_api_key: str = ""
    cerebras_api_key: str = ""
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    claude_api_key: str = ""
    gemini_api_key: str = ""
    google_api_key: str = ""
    aidot_groq_api_key: str = ""
    aidot_mistral_api_key: str = ""
    aidot_cerebras_api_key: str = ""
    aidot_openrouter_api_key: str = ""
    aidot_ollama_base_url: str = ""
    ollama_base_url: str = ""
    aidot_claude_base_url: str = ""
    aidot_gemini_base_url: str = ""
    aidot_chatgpt_base_url: str = ""
    aidot_groq_base_url: str = ""
    aidot_mistral_base_url: str = ""
    aidot_cerebras_base_url: str = ""
    aidot_openrouter_base_url: str = ""

    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/cga"
    db_connect_timeout_seconds: int = 1
    nlu_model_storage_path: str = "data/nlu_models"
    aidot_vector_worker_base_url: str = "http://localhost:8350"
    aidot_vector_embedding_timeout_seconds: float = 120.0
    answer_vector_index_timeout_seconds: float = 7200.0

    jwt_secret: str = "change-me"
    jwt_access_token_expires_minutes: int = 60

    initial_admin_login_id: str = "master"
    initial_admin_password: str = "master"

    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", ROOT_DIR / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_production_credentials(self) -> "Settings":
        if self.app_env.strip().lower() != "production":
            return self

        insecure_fields: list[str] = []
        if self.jwt_secret == "change-me" or len(self.jwt_secret) < 32:
            insecure_fields.append("JWT_SECRET")
        if self.initial_admin_password == "master" or len(self.initial_admin_password) < 12:
            insecure_fields.append("INITIAL_ADMIN_PASSWORD")
        if "postgres:postgres@" in self.sqlalchemy_database_url:
            insecure_fields.append("DATABASE_URL")

        if insecure_fields:
            joined_fields = ", ".join(insecure_fields)
            raise ValueError(f"Production credentials must be configured securely: {joined_fields}")
        return self

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def nlu_model_storage_dir(self) -> Path:
        path = Path(self.nlu_model_storage_path)
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def legacy_asset_root_path(self) -> Path | None:
        value = self.cga_legacy_asset_root.strip()
        return Path(value).expanduser().resolve(strict=False) if value else None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
