from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, DomainFloat, DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection
from app.domain.time.time_range import TimeRange


class UpstreamResultCellProjection(DomainModel):
    parameter: ParameterKey
    window_id: NonEmptyString
    time_range: TimeRange
    outcome_status: Literal[OutcomeStatus.QUANTIFIED, OutcomeStatus.DIRECTIONAL]
    representative_value: CanonicalDecimal | None
    unit_id: NonEmptyString | None
    trend: TrendDirection | None
    risk_level: RiskLevel
    source: ResultSource
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        quantified = self.outcome_status is OutcomeStatus.QUANTIFIED
        if quantified != (self.representative_value is not None):
            raise ValueError("upstream value must match outcome status")
        if quantified != (self.unit_id is not None):
            raise ValueError("upstream unit must match outcome status")
        return self
