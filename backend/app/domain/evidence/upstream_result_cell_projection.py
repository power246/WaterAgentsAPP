from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, DomainFloat, DomainModel, NonEmptyString
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


class UpstreamResultCellProjection(DomainModel):
    parameter: ParameterKey
    window_id: NonEmptyString
    time_range: TimeRange
    outcome_status: Literal[
        OutcomeStatus.QUANTIFIED, OutcomeStatus.RANGE_ESTIMATE, OutcomeStatus.DIRECTIONAL
    ]
    representative_value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    unit_id: NonEmptyString | None
    trend: TrendDirection | None
    risk_level: RiskLevel
    source: ResultSource
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    quality_flags: tuple[DataQualityFlag, ...] = ()
    inference_method: Literal["explicit_calculation", "model_estimate"] | None = None
    inference_basis: Literal[
        "same_parameter_measurement", "related_evidence", "general_knowledge"
    ] | None = None
    measured_anchors: tuple[MeasuredAnchor, ...] = ()

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        validate_result_range(
            self.outcome_status, self.representative_value,
            self.value_lower, self.value_upper,
        )
        validate_inference_metadata(
            self.outcome_status, self.inference_method, self.inference_basis,
            bool(self.measured_anchors), self.value_lower,
        )
        quantified = self.outcome_status is OutcomeStatus.QUANTIFIED
        if quantified != (self.representative_value is not None):
            raise ValueError("upstream value must match outcome status")
        numerical = self.outcome_status in {
            OutcomeStatus.QUANTIFIED, OutcomeStatus.RANGE_ESTIMATE
        }
        if numerical != (self.unit_id is not None):
            raise ValueError("upstream unit must match outcome status")
        if any(anchor.parameter != self.parameter for anchor in self.measured_anchors):
            raise ValueError("upstream measured anchor must match result parameter")
        if (
            self.inference_method == "model_estimate"
            or self.outcome_status is OutcomeStatus.RANGE_ESTIMATE
        ) and self.source is ResultSource.OBSERVED:
            raise ValueError("upstream model estimate cannot claim an observed source")
        return self
