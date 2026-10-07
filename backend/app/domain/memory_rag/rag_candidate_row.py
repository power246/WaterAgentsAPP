from typing import Annotated

from pydantic import Field

from app.domain.common import DomainFloat, DomainModel, NonEmptyString, UtcDatetime
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.rag_quality_tier import RAGQualityTier
from app.domain.memory_rag.rag_score_components import RAGScoreComponentsV1


class RAGCandidateRow(DomainModel):
    case_ref: HistoricalCaseRef
    projection_digest: NonEmptyString
    matched_by: RAGScoreComponentsV1
    rag_score: Annotated[DomainFloat, Field(ge=0, le=1)]
    matched_parameter_count: Annotated[int, Field(strict=True, ge=1)]
    quality_tier: RAGQualityTier
    original_confidence: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    source_completed_at: UtcDatetime
