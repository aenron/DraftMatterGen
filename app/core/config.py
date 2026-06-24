from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field("draft-reason-service", validation_alias="APP_NAME")
    app_env: str = Field("production", validation_alias="APP_ENV")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(False, validation_alias="LOG_JSON")

    api_key_enabled: bool = Field(False, validation_alias="API_KEY_ENABLED")
    service_api_key: SecretStr | None = Field(None, validation_alias="SERVICE_API_KEY")

    llm_base_url: str = Field("", validation_alias="LLM_BASE_URL")
    llm_api_key: SecretStr | None = Field(None, validation_alias="LLM_API_KEY")
    llm_model: str = Field("", validation_alias="LLM_MODEL")
    llm_chat_completions_path: str = Field(
        "/chat/completions", validation_alias="LLM_CHAT_COMPLETIONS_PATH"
    )
    llm_timeout_seconds: float = Field(60, gt=0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(2, ge=0, le=5, validation_alias="LLM_MAX_RETRIES")
    llm_temperature: float = Field(0.1, ge=0, le=2, validation_alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(300, gt=0, validation_alias="LLM_MAX_TOKENS")
    llm_response_format_json: bool = Field(True, validation_alias="LLM_RESPONSE_FORMAT_JSON")

    upload_max_mb: int = Field(20, gt=0, validation_alias="UPLOAD_MAX_MB")
    extract_max_chars: int = Field(50_000, gt=1000, validation_alias="EXTRACT_MAX_CHARS")
    max_document_chunks: int = Field(8, gt=0, le=50, validation_alias="MAX_DOCUMENT_CHUNKS")
    allowed_extensions: str = Field("docx,doc,txt", validation_alias="ALLOWED_EXTENSIONS")
    temp_dir: Path = Field(Path("/tmp/draft-reason"), validation_alias="TEMP_DIR")
    libreoffice_binary: str = Field("libreoffice", validation_alias="LIBREOFFICE_BINARY")
    conversion_timeout_seconds: float = Field(
        60, gt=0, validation_alias="CONVERSION_TIMEOUT_SECONDS"
    )
    async_queue_max_size: int = Field(
        100, gt=0, le=10_000, validation_alias="ASYNC_QUEUE_MAX_SIZE"
    )
    async_worker_count: int = Field(
        2, gt=0, le=32, validation_alias="ASYNC_WORKER_COUNT"
    )
    async_job_ttl_seconds: int = Field(
        3600, ge=60, validation_alias="ASYNC_JOB_TTL_SECONDS"
    )
    async_data_dir: Path = Field(Path("./data"), validation_alias="ASYNC_DATA_DIR")

    @property
    def allowed_extension_set(self) -> set[str]:
        return {
            value.strip().lower().lstrip(".")
            for value in self.allowed_extensions.split(",")
            if value.strip()
        }

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url.strip() and self.llm_model.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
