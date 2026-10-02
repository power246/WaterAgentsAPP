from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, NonEmptyString
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.item_reference import ItemReference
from app.domain.evidence.source_record_base import _SourceRecordBase


class HistoricalTranscriptRecord(_SourceRecordBase):
    record_type: Literal["historical_transcript"] = "historical_transcript"
    original_run_id: NonEmptyString
    original_session_id: NonEmptyString
    original_item_ref: ItemReference
    content_type: NonEmptyString
    content_schema_version: Annotated[int, Field(strict=True, ge=1)]
    rag_score: Annotated[DomainFloat, Field(ge=0, le=1)]
    matched_by: NonEmptyString
    original_quality: EvidenceQualityClass | None = None
    original_confidence: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def validate_historical_record(self) -> Self:
        if self.valid_time_range is None:
            raise ValueError("historical transcript requires valid_time_range")
        if self.raw_unit is not None:
            raise ValueError("historical transcript must not declare raw_unit")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        return (self.original_item_ref.item_id,)
