from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.model_catalog import ModelCapability

BACKEND_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ENV_FILE = BACKEND_ROOT / ".env"


class SettingsConfigurationError(RuntimeError):
    pass


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATERAGENTS_",
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        extra="forbid",
        str_strip_whitespace=True,
    )

    app_name: str = Field(default="WaterAgents API", min_length=1)
    environment: str = Field(default="development", min_length=1)
    log_level: Literal[
        "trace",
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    ] = "info"
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    analysis_timezone: str = "America/Chicago"
    model_catalog: tuple[ModelCapability, ...] = ()
    topology_max_distance_km: float = Field(
        default=100,
        gt=0,
        le=1000,
        allow_inf_nan=False,
    )
    topology_max_hops: int = Field(default=12, ge=1, le=100)
    topology_max_nodes: int = Field(default=128, ge=1, le=10000)
    topology_max_concurrency: int = Field(default=8, ge=1, le=64)
    waterbody_workflow_max_concurrency: int = Field(default=4, ge=1, le=32)
    prediction_baseline_hours: int = Field(default=48, ge=1, le=168)
    max_target_parameters: int = Field(default=6, ge=1, le=6)
    query_parser_timeout_seconds: float = Field(
        default=45,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    geocoding_timeout_seconds: float = Field(
        default=10,
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    metadata_timeout_seconds: float = Field(
        default=15,
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    provider_http_max_response_bytes: int = Field(
        default=4_194_304,
        ge=1024,
        le=67_108_864,
    )
    provider_http_page_size: int = Field(default=1000, ge=1, le=50_000)
    provider_http_max_pages: int = Field(default=10, ge=1, le=100)
    provider_http_max_records: int = Field(default=10_000, ge=1, le=50_000)
    provider_http_retry_delay_seconds: float = Field(
        default=0.25,
        ge=0,
        le=10,
        allow_inf_nan=False,
    )
    evidence_provider_timeout_seconds: float = Field(
        default=20,
        gt=0,
        le=20,
        allow_inf_nan=False,
    )
    evidence_provider_attempts: int = Field(default=2, ge=1, le=2)
    evidence_total_deadline_seconds: float = Field(
        default=90,
        gt=0,
        le=90,
        allow_inf_nan=False,
    )
    evidence_max_collection_calls: int = Field(default=12, ge=1, le=12)
    evidence_max_transform_calls: int = Field(default=24, ge=1, le=24)
    evidence_max_pages: int = Field(default=10, ge=1, le=10)
    evidence_max_records: int = Field(default=10_000, ge=1, le=10_000)
    evidence_max_response_bytes: int = Field(
        default=8_388_608,
        ge=1024,
        le=8_388_608,
    )
    evidence_inspection_page_size: int = Field(default=50, ge=1, le=200)
    evidence_inspection_max_page_size: int = Field(default=200, ge=1, le=200)
    evidence_preview_max_records: int = Field(default=32, ge=1, le=32)
    evidence_preview_max_bytes: int = Field(default=16_384, ge=1024, le=16_384)
    evidence_facility_max_centers: int = Field(default=32, ge=1, le=32)
    reasoning_max_planning_turns: int = Field(default=6, ge=1, le=6)
    reasoning_max_tool_calls_per_turn: int = Field(default=4, ge=1, le=4)
    reasoning_max_cells_per_batch: int = Field(default=32, ge=1, le=32)
    reasoning_max_batches: int = Field(default=24, ge=1, le=24)
    reasoning_model_timeout_seconds: float = Field(
        default=90,
        gt=0,
        le=90,
        allow_inf_nan=False,
    )
    reasoning_session_deadline_seconds: float = Field(
        default=600,
        gt=0,
        le=600,
        allow_inf_nan=False,
    )
    run_summary_timeout_seconds: float = Field(
        default=120,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    nws_user_agent_project: str = Field(default="WaterAgents", min_length=1)
    nws_user_agent_contact: str = Field(
        default="wateragents@example.invalid",
        min_length=1,
    )
    google_geocoding_api_key: SecretStr | None = None
    usgs_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        min_length=1,
    )
    topology_stage_timeout_seconds: float = Field(
        default=120,
        gt=0,
        le=900,
        allow_inf_nan=False,
    )
    db_host: str = Field(default="127.0.0.1", min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="wateragents", min_length=1)
    db_user: str = Field(default="wateragents", min_length=1)
    db_password: SecretStr | None = None
    db_sslmode: Literal[
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    ] = "disable"
    db_sslrootcert: str | None = None
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)
    db_operation_timeout_seconds: float = Field(
        default=5,
        gt=0,
        allow_inf_nan=False,
    )
    idempotency_wait_timeout_seconds: float = Field(
        default=5,
        gt=0,
        allow_inf_nan=False,
    )
    persistence_batch_items: int = Field(default=100, gt=0)
    transcript_inline_max_bytes: int = Field(default=65536, gt=0)
    transcript_preview_bytes: int = Field(default=2048, gt=0)
    live_state_grace_seconds: float = Field(
        default=60,
        ge=0,
        allow_inf_nan=False,
    )
    shutdown_save_grace_seconds: float = Field(
        default=5,
        ge=0,
        allow_inf_nan=False,
    )
    rag_discovery_batch_size: int = Field(default=64, ge=1, le=1000)
    rag_reconciliation_max_batches: int = Field(default=4, ge=1, le=100)
    rag_worker_claim_batch_size: int = Field(default=16, ge=1, le=128)
    rag_embedding_batch_size: int = Field(default=16, ge=1, le=128)
    rag_worker_max_concurrency: int = Field(default=1, ge=1, le=8)
    rag_embedding_timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=120,
        allow_inf_nan=False,
    )
    rag_job_lease_seconds: float = Field(
        default=60,
        gt=0,
        le=600,
        allow_inf_nan=False,
    )
    rag_job_backoff_base_seconds: float = Field(
        default=1,
        gt=0,
        le=60,
        allow_inf_nan=False,
    )
    rag_job_backoff_max_seconds: float = Field(
        default=60,
        gt=0,
        le=900,
        allow_inf_nan=False,
    )
    rag_reconciliation_interval_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        allow_inf_nan=False,
    )
    rag_worker_shutdown_grace_seconds: float = Field(
        default=5,
        ge=0,
        le=120,
        allow_inf_nan=False,
    )
    rag_retired_generation_retention_seconds: float = Field(
        default=604800,
        ge=0,
        le=31536000,
        allow_inf_nan=False,
    )

    @field_validator(
        "db_password",
        "google_geocoding_api_key",
        "usgs_api_key",
        "openai_api_key",
        mode="before",
    )
    @classmethod
    def preserve_secret(cls, value: object) -> object:
        if isinstance(value, str):
            return SecretStr(value)
        return value

    @field_validator(
        "topology_max_hops",
        "topology_max_nodes",
        "topology_max_concurrency",
        "waterbody_workflow_max_concurrency",
        "prediction_baseline_hours",
        "max_target_parameters",
        "provider_http_max_response_bytes",
        "provider_http_page_size",
        "provider_http_max_pages",
        "provider_http_max_records",
        "evidence_provider_attempts",
        "evidence_max_collection_calls",
        "evidence_max_transform_calls",
        "evidence_max_pages",
        "evidence_max_records",
        "evidence_max_response_bytes",
        "evidence_inspection_page_size",
        "evidence_inspection_max_page_size",
        "evidence_preview_max_records",
        "evidence_preview_max_bytes",
        "evidence_facility_max_centers",
        "reasoning_max_planning_turns",
        "reasoning_max_tool_calls_per_turn",
        "reasoning_max_cells_per_batch",
        "reasoning_max_batches",
        "rag_discovery_batch_size",
        "rag_reconciliation_max_batches",
        "rag_worker_claim_batch_size",
        "rag_embedding_batch_size",
        "rag_worker_max_concurrency",
        mode="before",
    )
    @classmethod
    def reject_non_integer_limits(cls, value: object) -> object:
        if isinstance(value, (bool, float)):
            raise ValueError("integer limits require an integer value")
        return value

    @field_validator(
        "topology_max_distance_km",
        "query_parser_timeout_seconds",
        "geocoding_timeout_seconds",
        "metadata_timeout_seconds",
        "provider_http_retry_delay_seconds",
        "evidence_provider_timeout_seconds",
        "evidence_total_deadline_seconds",
        "reasoning_model_timeout_seconds",
        "reasoning_session_deadline_seconds",
        "run_summary_timeout_seconds",
        "topology_stage_timeout_seconds",
        "rag_embedding_timeout_seconds",
        "rag_job_lease_seconds",
        "rag_job_backoff_base_seconds",
        "rag_job_backoff_max_seconds",
        "rag_reconciliation_interval_seconds",
        "rag_worker_shutdown_grace_seconds",
        "rag_retired_generation_retention_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("numeric settings must not be boolean")
        return value

    @field_validator("db_sslmode", mode="before")
    @classmethod
    def normalize_sslmode(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("db_sslrootcert", mode="before")
    @classmethod
    def normalize_sslrootcert(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("openai_base_url")
    @classmethod
    def normalize_openai_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("openai_base_url must be an HTTP(S) URL")
        return normalized

    @field_validator("nws_user_agent_project", "nws_user_agent_contact")
    @classmethod
    def validate_nws_user_agent_part(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("NWS User-Agent values must be single-line text")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(f"Unknown IANA timezone: {value}") from error
        return value

    @model_validator(mode="after")
    def validate_transcript_thresholds(self) -> "AppSettings":
        if self.transcript_preview_bytes % 2:
            raise ValueError("transcript_preview_bytes must be even")
        if self.transcript_preview_bytes >= self.transcript_inline_max_bytes:
            raise ValueError(
                "transcript_preview_bytes must be smaller than transcript_inline_max_bytes"
            )
        if self.provider_http_page_size > self.provider_http_max_records:
            raise ValueError(
                "provider_http_page_size must not exceed provider_http_max_records"
            )
        if self.evidence_inspection_page_size > self.evidence_inspection_max_page_size:
            raise ValueError(
                "evidence_inspection_page_size must not exceed "
                "evidence_inspection_max_page_size"
            )
        if (
            self.reasoning_model_timeout_seconds
            > self.reasoning_session_deadline_seconds
        ):
            raise ValueError(
                "reasoning_model_timeout_seconds must not exceed "
                "reasoning_session_deadline_seconds"
            )
        if self.rag_embedding_batch_size > self.rag_worker_claim_batch_size:
            raise ValueError(
                "rag_embedding_batch_size must not exceed rag_worker_claim_batch_size"
            )
        if self.rag_job_lease_seconds <= self.rag_embedding_timeout_seconds:
            raise ValueError(
                "rag_job_lease_seconds must exceed rag_embedding_timeout_seconds"
            )
        if self.rag_job_backoff_max_seconds < self.rag_job_backoff_base_seconds:
            raise ValueError(
                "rag_job_backoff_max_seconds must not be below "
                "rag_job_backoff_base_seconds"
            )
        return self


def load_app_settings() -> AppSettings:
    try:
        return AppSettings()
    except ValidationError as error:
        fields = sorted(
            {
                ".".join(str(part) for part in issue["loc"])
                for issue in error.errors(include_url=False, include_input=False)
            }
        )
        detail = ", ".join(fields) if fields else "unknown field"
        raise SettingsConfigurationError(
            f"invalid application configuration ({detail})"
        ) from None
