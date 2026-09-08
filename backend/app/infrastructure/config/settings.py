from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATERAGENTS_",
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
