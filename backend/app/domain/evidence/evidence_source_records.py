from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.collection_manifest import CollectionManifest
from app.domain.evidence.source_record import SourceRecord


class EvidenceSourceRecords(DomainModel):
    schema_version: Literal[1] = 1
    operation_id: NonEmptyString
    batch_id: NonEmptyString
    manifest: CollectionManifest
    records: tuple[SourceRecord, ...] = ()

    @field_validator("records")
    @classmethod
    def validate_unique_records(
        cls,
        value: tuple[SourceRecord, ...],
    ) -> tuple[SourceRecord, ...]:
        ensure_unique_tuple(
            tuple(item.record_id for item in value),
            field_name="source record identities",
        )
        return value

    @model_validator(mode="after")
    def validate_manifest_identity(self) -> Self:
        if self.batch_id != self.manifest.manifest_id:
            raise ValueError("source record batch_id must equal manifest_id")
        return self
