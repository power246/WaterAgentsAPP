from pathlib import Path
from typing import Literal
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
    google_geocoding_api_key: SecretStr | None = None
    usgs_api_key: SecretStr | None = None
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

    @field_validator(
        "db_password",
        "google_geocoding_api_key",
        "usgs_api_key",
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
        "prediction_baseline_hours",
        "max_target_parameters",
        "provider_http_max_response_bytes",
        "provider_http_page_size",
        "provider_http_max_pages",
        "provider_http_max_records",
        mode="before",
    )
    @classmethod
    def reject_non_integer_m4_limits(cls, value: object) -> object:
        if isinstance(value, (bool, float)):
            raise ValueError("M4 integer limits require an integer value")
        return value

    @field_validator(
        "topology_max_distance_km",
        "query_parser_timeout_seconds",
        "geocoding_timeout_seconds",
        "metadata_timeout_seconds",
        "provider_http_retry_delay_seconds",
        "topology_stage_timeout_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_m4_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("M4 numeric settings must not be boolean")
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
