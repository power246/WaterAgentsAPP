from datetime import date
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, SourceId


class GuidelineSourceRef(DomainModel):
    source_id: SourceId
    title: Annotated[NormalizedText, Field(min_length=1, max_length=240)]
    url: Annotated[str, Field(max_length=2048)] | None = None
    locator: Annotated[NormalizedText, Field(max_length=240)] | None = None
    published_date: date | None = None
    accessed_date: date | None = None

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source title must not be blank")
        return value

    @field_validator("url")
    @classmethod
    def safe_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("source URL must be a string")
        try:
            value.encode("utf-8")
            parsed = urlsplit(value)
            port = parsed.port
        except (ValueError, UnicodeEncodeError):
            raise ValueError("source URL must be a valid HTTP(S) URL") from None
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value or (port is not None and port == 0)
            or any(key.casefold() in {"api_key", "token", "access_token", "password"}
                   for key, _ in parse_qsl(parsed.query))
        ):
            raise ValueError("source URL must be HTTP(S) without credentials or whitespace")
        return value
