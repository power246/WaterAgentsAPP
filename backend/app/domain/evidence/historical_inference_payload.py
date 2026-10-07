from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.historical_supporting_fragment_reference import (
    HistoricalSupportingFragmentReference,
)
from app.domain.memory_rag import HistoricalCaseRef, HistoricalResultCellProjection


class HistoricalInferencePayload(DomainModel):
    payload_type: Literal["historical_inference"] = "historical_inference"
    case_ref: HistoricalCaseRef
    historical_window_id: NonEmptyString
    cell_projection: HistoricalResultCellProjection
    supporting_fragment_refs: Annotated[
        tuple[HistoricalSupportingFragmentReference, ...],
        Field(max_length=2),
    ] = ()

    @model_validator(mode="after")
    def validate_window_identity(self) -> "HistoricalInferencePayload":
        if self.historical_window_id != self.cell_projection.window_id:
            raise ValueError("historical window identity must match cell projection")
        if len(self.supporting_fragment_refs) != len(set(self.supporting_fragment_refs)):
            raise ValueError("historical supporting references must not contain duplicates")
        return self
