import json
import re
import unicodedata
from hashlib import sha256
from typing import Annotated

from pydantic import BeforeValidator, Field


def normalize_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("text contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("text must be valid UTF-8") from None
    return value


def canonical_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return "sha256:" + sha256(canonical_text(value).encode("utf-8")).hexdigest()


def require_safe_model_text(value: str) -> str:
    lowered = value.casefold()
    markers = (
        "authorization:", "access_token", "api_key", "api key", "api-key",
        "credential", "password", "refresh_token", "raw_provider_envelope",
        "encrypted_reasoning", "/api/guidelines",
    )
    if any(marker in lowered for marker in markers) or re.search(
        r"[a-z][a-z0-9+.-]*://|<[/!a-z]|\b(?:javascript|data|mailto):(?=\S)"
        r"|[\[(<]\s*(?:javascript|data|mailto)\s*:", lowered
    ):
        raise ValueError("model text contains an unsafe URL, secret marker or HTML")
    return value


NormalizedText = Annotated[str, BeforeValidator(normalize_text)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
SourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Revision = Annotated[int, Field(strict=True, ge=0)]
VersionNumber = Annotated[int, Field(strict=True, ge=1)]


def unique_parameters(values):
    return tuple(sorted(set(values), key=lambda item: (item.code_system, item.code)))


def unique_texts(value: object) -> object:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(sorted({normalize_text(item) for item in value}))
    return value


MAX_GUIDELINE_DOCUMENT_BYTES = 24_000
