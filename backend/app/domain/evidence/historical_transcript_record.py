from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, NonEmptyString
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.historical_supporting_fragment_projection import (
    HistoricalSupportingFragmentProjection,
)
from app.domain.evidence.source_record_base import _SourceRecordBase
from app.domain.memory_rag import (
    HistoricalCaseRef,
    HistoricalResultProjection,
    RAGScoreComponentsV1,
)


class HistoricalTranscriptRecord(_SourceRecordBase):
    record_type: Literal["historical_transcript"] = "historical_transcript"
    provider: Literal["wateragents_transcript"]
    channel: Literal["historical_memory"]
    source_uri: None
    case_ref: HistoricalCaseRef
    content_type: Literal["inference_result"] = "inference_result"
    content_schema_version: Literal[1] = 1
    projection_schema_version: Literal[1] = 1
    profile_id: NonEmptyString
    projection_id: NonEmptyString
    projection_digest: NonEmptyString
    rag_score: Annotated[DomainFloat, Field(ge=0, le=1)]
    matched_by: RAGScoreComponentsV1
    original_quality: Literal[
        EvidenceQualityClass.VALID,
        EvidenceQualityClass.SUSPICIOUS,
    ]
    original_confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    result_projection: HistoricalResultProjection
    supporting_fragments: Annotated[
        tuple[HistoricalSupportingFragmentProjection, ...],
        Field(max_length=2),
    ] = ()

    @model_validator(mode="after")
    def validate_historical_record(self) -> Self:
        if self.valid_time_range is None:
            raise ValueError("historical transcript requires valid_time_range")
        if self.raw_unit is not None:
            raise ValueError("historical transcript must not declare raw_unit")
        if self.location.comid != self.case_ref.target_comid:
            raise ValueError("historical location must match case target COMID")
        if any(
            cell.time_range.start < self.valid_time_range.start
            or cell.time_range.end > self.valid_time_range.end
            for cell in self.result_projection.cells
        ):
            raise ValueError("historical cells must fit the record valid time range")
        fragment_identities = tuple(
            (item.original_item_id, item.evidence_id)
            for item in self.supporting_fragments
        )
        if len(fragment_identities) != len(set(fragment_identities)):
            raise ValueError("historical supporting fragments must not contain duplicates")
        if any(
            not any(
                item.parameter == cell.parameter
                and item.valid_time_range == cell.time_range
                for cell in self.result_projection.cells
            )
            for item in self.supporting_fragments
        ):
            raise ValueError("historical fragment time range must match an authorized cell")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        # These references cross run boundaries and are validated by the M8 hydrator.
        return ()
