from typing import Annotated, Literal

from pydantic import Field

from app.domain.common import DomainModel
from app.domain.evidence.historical_supporting_fragment_projection import (
    HistoricalSupportingFragmentProjection,
)
from app.domain.memory_rag import HistoricalCaseRef, HistoricalResultProjection


class HistoricalCaseSourceV1(DomainModel):
    case_ref: HistoricalCaseRef
    content_type: Literal["inference_result"] = "inference_result"
    content_schema_version: Literal[1] = 1
    projection_schema_version: Literal[1] = 1
    result_projection: HistoricalResultProjection
    supporting_fragments: Annotated[
        tuple[HistoricalSupportingFragmentProjection, ...],
        Field(max_length=2),
    ] = ()
