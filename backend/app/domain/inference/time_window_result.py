from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainFloat,
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.measured_anchor import MeasuredAnchor
from app.domain.inference.aggregation_method import AggregationMethod
from app.domain.inference.assessment import RiskOrTrendAssessment
from app.domain.inference.indeterminate_reason import IndeterminateReason
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection
from app.domain.inference.value_range import (
    validate_inference_metadata,
    validate_result_range,
)


class TimeWindowResult(DomainModel):
    cell_id: NonEmptyString
    start_time: UtcDatetime
    end_time: UtcDatetime
    outcome_status: OutcomeStatus
    representative_value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    unit_id: NonEmptyString | None
    assessment: RiskOrTrendAssessment
    source: ResultSource | None
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    confidence_rationale: NonEmptyString
    aggregation_method: AggregationMethod | None
    evidence_refs: tuple[NonEmptyString, ...]
    quality_flags: tuple[DataQualityFlag, ...] = ()
    temporal_estimate_method: Literal["linear_interpolation", "nearest_hold"] | None = None
    inference_method: Literal["explicit_calculation", "model_estimate"] | None = None
    inference_basis: Literal[
        "same_parameter_measurement", "related_evidence", "general_knowledge",
    ] | None = None
    measured_anchors: tuple[MeasuredAnchor, ...] = ()

    @field_validator("representative_value", "value_lower", "value_upper", mode="before")
    @classmethod
    def validate_representative_value_type(
        cls,
        value: object,
    ) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("result values must be decimal strings")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        if not self.evidence_refs:
            raise ValueError("evidence_refs must not be empty")
        ensure_unique_tuple(self.evidence_refs, field_name="evidence_refs")
        validate_result_range(
            self.outcome_status, self.representative_value,
            self.value_lower, self.value_upper,
        )
        validate_inference_metadata(
            self.outcome_status, self.inference_method, self.inference_basis,
            bool(self.measured_anchors), self.value_lower,
        )
        if self.inference_method == "model_estimate" and self.source is ResultSource.OBSERVED:
            raise ValueError("model estimate cannot claim an observed source")
        if self.temporal_estimate_method is not None and (
            self.outcome_status is not OutcomeStatus.QUANTIFIED
            or self.source is not ResultSource.INFERRED
            or DataQualityFlag.TEMPORAL_ESTIMATE not in self.quality_flags
        ):
            raise ValueError("temporal estimate method requires an inferred quantified value")

        unknown_supporting_refs = (
            set(self.assessment.supporting_refs) - set(self.evidence_refs)
        )
        if unknown_supporting_refs:
            raise ValueError(
                "assessment supporting_refs must be included in evidence_refs"
            )

        reason_codes = self.assessment.reason_codes
        if self.outcome_status is OutcomeStatus.QUANTIFIED:
            if self.representative_value is None:
                raise ValueError("quantified outcome requires representative_value")
            if self.unit_id is None:
                raise ValueError("quantified outcome requires unit_id")
            if self.source is None:
                raise ValueError("quantified outcome requires source")
            if reason_codes:
                raise ValueError("reason_codes are only allowed for indeterminate outcome")
            if (
                self.source is ResultSource.OBSERVED
                and self.aggregation_method is not None
            ):
                raise ValueError("observed outcome must not include aggregation_method")
        elif self.outcome_status is OutcomeStatus.RANGE_ESTIMATE:
            if self.representative_value is not None:
                raise ValueError("range estimate must not include representative_value")
            if self.unit_id is None or self.source not in (
                ResultSource.INFERRED, ResultSource.PREDICTED,
            ):
                raise ValueError("range estimate requires unit and inferred or predicted source")
            if self.aggregation_method is not None or reason_codes:
                raise ValueError("range estimate cannot include aggregation or reason codes")
        elif self.outcome_status is OutcomeStatus.DIRECTIONAL:
            if self.representative_value is not None:
                raise ValueError("directional outcome must not include representative_value")
            if self.unit_id is not None:
                raise ValueError("directional outcome must not include unit_id")
            if self.source is None:
                raise ValueError("directional outcome requires source")
            if self.aggregation_method is not None:
                raise ValueError("directional outcome must not include aggregation_method")
            has_definite_trend = self.assessment.trend not in (
                None,
                TrendDirection.UNCERTAIN,
            )
            has_definite_risk = self.assessment.risk_level not in (
                RiskLevel.INDETERMINATE,
            )
            if not has_definite_trend and not has_definite_risk:
                raise ValueError("directional outcome requires a definite trend or risk")
            if reason_codes:
                raise ValueError("reason_codes are only allowed for indeterminate outcome")
        else:
            if self.representative_value is not None:
                raise ValueError(
                    "indeterminate outcome must not include representative_value"
                )
            if self.unit_id is not None:
                raise ValueError("indeterminate outcome must not include unit_id")
            if self.source is not None:
                raise ValueError("indeterminate outcome source must be null")
            if self.aggregation_method is not None:
                raise ValueError(
                    "indeterminate outcome must not include aggregation_method"
                )
            if not reason_codes:
                raise ValueError(
                    "indeterminate outcome requires reason_codes"
                )
            if self.assessment.risk_level is not RiskLevel.INDETERMINATE:
                raise ValueError("indeterminate outcome requires indeterminate risk")
            if (
                IndeterminateReason.INSUFFICIENT_EVIDENCE in reason_codes
                and self.confidence != 0
            ):
                raise ValueError("fixed insufficient outcome requires confidence 0")
        return self
