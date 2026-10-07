from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import CanonicalDecimal, DomainFloat, DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection
from app.domain.time.time_range import TimeRange


class HistoricalResultCellProjection(DomainModel):
    parameter: ParameterKey
    window_id: NonEmptyString
    time_range: TimeRange
    outcome_status: OutcomeStatus
    representative_value: CanonicalDecimal | None = None
    unit_id: NonEmptyString | None = None
    trend: TrendDirection | None
    risk_level: RiskLevel
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    source: ResultSource | None

    @field_validator("representative_value", mode="before")
    @classmethod
    def validate_representative_value_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("representative_value must be a decimal string")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome_status is OutcomeStatus.QUANTIFIED:
            if self.representative_value is None or self.unit_id is None:
                raise ValueError("quantified historical cell requires value and unit")
            if self.source is None:
                raise ValueError("quantified historical cell requires source")
        elif self.outcome_status is OutcomeStatus.DIRECTIONAL:
            if self.representative_value is not None or self.unit_id is not None:
                raise ValueError("directional historical cell must not include value or unit")
            if self.source is None:
                raise ValueError("directional historical cell requires source")
            if self.trend in {None, TrendDirection.UNCERTAIN} and (
                self.risk_level is RiskLevel.INDETERMINATE
            ):
                raise ValueError("directional historical cell requires a definite outcome")
        else:
            if (
                self.representative_value is not None
                or self.unit_id is not None
                or self.source is not None
            ):
                raise ValueError("indeterminate historical cell cannot include value or source")
            if self.risk_level is not RiskLevel.INDETERMINATE or self.confidence != 0:
                raise ValueError("indeterminate historical cell requires zero confidence")
            if self.trend not in {None, TrendDirection.UNCERTAIN}:
                raise ValueError("indeterminate historical cell cannot have a trend")
        return self
