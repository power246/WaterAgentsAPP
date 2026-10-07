from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainModel, UtcDatetime, ensure_unique_tuple
from app.domain.inference.assessment_availability import AssessmentAvailability
from app.domain.inference.mode import InferenceMode
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.risk_level import RiskLevel
from app.domain.memory_rag.historical_result_cell_projection import (
    HistoricalResultCellProjection,
)


class HistoricalResultProjection(DomainModel):
    mode: InferenceMode
    source_completed_at: UtcDatetime
    overall_risk: RiskLevel
    availability: AssessmentAvailability
    cells: Annotated[
        tuple[HistoricalResultCellProjection, ...],
        Field(min_length=1, max_length=200),
    ]

    @field_validator("cells")
    @classmethod
    def validate_unique_cells(
        cls,
        value: tuple[HistoricalResultCellProjection, ...],
    ) -> tuple[HistoricalResultCellProjection, ...]:
        identities = tuple((item.parameter, item.window_id) for item in value)
        ensure_unique_tuple(identities, field_name="historical result cells")
        return value

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        indeterminate = tuple(
            cell
            for cell in self.cells
            if cell.outcome_status is OutcomeStatus.INDETERMINATE
        )
        if self.availability is AssessmentAvailability.COMPLETE and indeterminate:
            raise ValueError("complete historical result cannot contain missing cells")
        if self.availability is AssessmentAvailability.INDETERMINATE:
            if len(indeterminate) != len(self.cells):
                raise ValueError("indeterminate historical result requires all cells missing")
            if self.overall_risk is not RiskLevel.INDETERMINATE:
                raise ValueError("indeterminate historical result requires indeterminate risk")
        return self
