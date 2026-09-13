from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ENV_FILE = BACKEND_ROOT / ".env"


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATERAGENTS_",
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
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

    @field_validator("db_password", mode="before")
    @classmethod
    def preserve_database_password(cls, value: object) -> object:
        if isinstance(value, str):
            return SecretStr(value)
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
        return self
