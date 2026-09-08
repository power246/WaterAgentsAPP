from collections.abc import Mapping

from pydantic import Field

from app.domain.common import DomainModel, JsonValue, NonEmptyString, UtcDatetime


class Provenance(DomainModel):
    provider: NonEmptyString
    retrieved_at: UtcDatetime
    source_uri: NonEmptyString | None
    source_record_ids: tuple[NonEmptyString, ...] = ()
    source_timezone: NonEmptyString | None
    source_utc_offset: NonEmptyString | None
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
