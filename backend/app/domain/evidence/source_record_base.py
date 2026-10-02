from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from typing import Self

from pydantic import BaseModel, model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.evidence.evidence_location import EvidenceLocation
from app.domain.evidence.provenance import Provenance
from app.domain.evidence.provider_quality import ProviderQuality
from app.domain.time.time_range import TimeRange

_AUTO_RECORD_ID = "__auto__"


def _canonical_identity_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_identity_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity datetime must include timezone information")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_identity_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_identity_value(item) for item in value]
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("identity numbers must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError(f"unsupported identity value: {type(value).__name__}")


def _canonical_record_id(values: Mapping[object, object]) -> str:
    identity_values = {
        str(key): value
        for key, value in values.items()
        if str(key) != "record_id"
    }
    canonical = json.dumps(
        _canonical_identity_value(identity_values),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(canonical).hexdigest()}"


class _SourceRecordBase(DomainModel):
    record_type: NonEmptyString
    record_id: NonEmptyString
    provider: NonEmptyString
    channel: NonEmptyString
    source_uri: NonEmptyString | None
    retrieved_at: UtcDatetime
    observed_time: UtcDatetime | None = None
    valid_time_range: TimeRange | None = None
    location: EvidenceLocation
    provider_quality: ProviderQuality
    raw_unit: NonEmptyString | None = None
    provenance: Provenance

    @model_validator(mode="before")
    @classmethod
    def populate_and_validate_record_id(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        values = dict(value)
        if values.get("record_id") is None:
            values["record_id"] = _AUTO_RECORD_ID
        return values

    @model_validator(mode="after")
    def validate_record_time(self) -> Self:
        if (self.observed_time is None) == (self.valid_time_range is None):
            raise ValueError(
                "exactly one of observed_time or valid_time_range is required"
            )
        identity_values = self.model_dump(mode="python", exclude={"record_id"})
        expected = self.provenance.provider_record_id or _canonical_record_id(
            identity_values
        )
        if self.record_id == _AUTO_RECORD_ID:
            object.__setattr__(self, "record_id", expected)
        elif self.record_id != expected:
            raise ValueError("record_id does not match canonical source identity")
        return self
