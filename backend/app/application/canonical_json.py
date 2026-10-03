import json
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from uuid import UUID

from app.domain.domain_model import DomainModel


def canonical_json_text(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_digest(value: object) -> str:
    return f"sha256:{sha256(canonical_json_text(value).encode('utf-8')).hexdigest()}"


def _json_value(value: object) -> object:
    if isinstance(value, DomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetime must include timezone information")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
