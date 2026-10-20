from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.measured_anchor import MeasuredAnchor
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection
from app.domain.inference.value_range import (
    validate_inference_metadata,
    validate_result_range,
)
from app.domain.time.time_range import TimeRange


class HistoricalResultCellProjection(DomainModel):
    parameter: ParameterKey
    window_id: NonEmptyString
    time_range: TimeRange
    outcome_status: OutcomeStatus
    representative_value: CanonicalDecimal | None = None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    unit_id: NonEmptyString | None = None
    trend: TrendDirection | None
    risk_level: RiskLevel
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    source: ResultSource | None
    quality_flags: tuple[DataQualityFlag, ...] = ()
    inference_method: Literal["explicit_calculation", "model_estimate"] | None = None
    inference_basis: Literal[
        "same_parameter_measurement", "related_evidence", "general_knowledge",
    ] | None = None
    measured_anchors: tuple[MeasuredAnchor, ...] = ()

    @field_validator("quality_flags")
    @classmethod
    def validate_quality_flags(
        cls,
        value: tuple[DataQualityFlag, ...],
    ) -> tuple[DataQualityFlag, ...]:
        return ensure_unique_tuple(value, field_name="historical cell quality flags")

    @field_validator("representative_value", "value_lower", "value_upper", mode="before")
    @classmethod
    def validate_representative_value_type(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("result values must be decimal strings")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        validate_result_range(
            self.outcome_status, self.representative_value,
            self.value_lower, self.value_upper,
        )
        validate_inference_metadata(
            self.outcome_status, self.inference_method, self.inference_basis,
            bool(self.measured_anchors), self.value_lower,
        )
        if self.inference_method == "model_estimate" and self.source is ResultSource.OBSERVED:
            raise ValueError("historical model estimate cannot claim an observed source")
        if any(anchor.parameter != self.parameter for anchor in self.measured_anchors):
            raise ValueError("historical measured anchors must match the cell parameter")
        if self.outcome_status is OutcomeStatus.QUANTIFIED:
            if self.representative_value is None or self.unit_id is None:
                raise ValueError("quantified historical cell requires value and unit")
            if self.source is None:
                raise ValueError("quantified historical cell requires source")
        elif self.outcome_status is OutcomeStatus.RANGE_ESTIMATE:
            if self.representative_value is not None or self.unit_id is None:
                raise ValueError("historical range estimate requires unit and no point value")
            if self.source not in (ResultSource.INFERRED, ResultSource.PREDICTED):
                raise ValueError("historical range estimate requires inferred or predicted source")
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
