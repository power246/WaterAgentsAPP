from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.source_record_reference import SourceRecordReference


class CollectionFactPayload(DomainModel):
    payload_type: Literal["collection_fact"] = "collection_fact"
    status: CollectionStatus
    reason: NonEmptyString
    affected_window_ids: tuple[NonEmptyString, ...]
    record_refs: tuple[SourceRecordReference, ...] = ()

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if not self.affected_window_ids:
            raise ValueError("collection fact requires affected_window_ids")
        ensure_unique_tuple(
            self.affected_window_ids,
            field_name="collection fact affected window IDs",
        )
        ensure_unique_tuple(self.record_refs, field_name="collection fact record refs")
        return self
