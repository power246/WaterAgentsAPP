from typing import Annotated, Self

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
from app.domain.inference.aggregation_method import AggregationMethod
from app.domain.inference.assessment import RiskOrTrendAssessment
from app.domain.inference.indeterminate_reason import IndeterminateReason
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection


class TimeWindowResult(DomainModel):
    cell_id: NonEmptyString
    start_time: UtcDatetime
    end_time: UtcDatetime
    outcome_status: OutcomeStatus
    representative_value: CanonicalDecimal | None
    unit_id: NonEmptyString | None
    assessment: RiskOrTrendAssessment
    source: ResultSource | None
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    confidence_rationale: NonEmptyString
    aggregation_method: AggregationMethod | None
    evidence_refs: tuple[NonEmptyString, ...]
    quality_flags: tuple[DataQualityFlag, ...] = ()

    @field_validator("representative_value", mode="before")
    @classmethod
    def validate_representative_value_type(
        cls,
        value: object,
    ) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("representative_value must be a decimal string")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        if not self.evidence_refs:
            raise ValueError("evidence_refs must not be empty")
        ensure_unique_tuple(self.evidence_refs, field_name="evidence_refs")

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
