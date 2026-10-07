from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.rag_quality_tier import RAGQualityTier
from app.domain.time.time_range import TimeRange


class RAGFragmentProjectionV1(DomainModel):
    case_ref: HistoricalCaseRef
    projection_schema_version: Literal[1] = 1
    source_content_type: Literal["evidence_fragment"] = "evidence_fragment"
    source_content_schema_version: Literal[1] = 1
    source_fragment_item_id: NonEmptyString
    evidence_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    valid_time_range: TimeRange
    quality_tier: RAGQualityTier
    applicability: Literal[
        "direct_measurement",
        "allowed_indirect",
        "contextual_only",
    ]
    provenance_identity: NonEmptyString
    projection_digest: NonEmptyString
