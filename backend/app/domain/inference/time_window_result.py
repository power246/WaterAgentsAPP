from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.inference.aggregation_method import AggregationMethod
from app.domain.inference.assessment import RiskOrTrendAssessment
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.result_source import ResultSource
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection


class TimeWindowResult(DomainModel):
    start_time: UtcDatetime
    end_time: UtcDatetime
    outcome_status: OutcomeStatus
    representative_value: DomainFloat | None
    assessment: RiskOrTrendAssessment | None
    source: ResultSource
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    aggregation_method: AggregationMethod | None
    evidence_refs: tuple[NonEmptyString, ...]
    quality_flags: tuple[DataQualityFlag, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        if not self.evidence_refs:
            raise ValueError("evidence_refs must not be empty")
        ensure_unique_tuple(self.evidence_refs, field_name="evidence_refs")

        reason_codes = () if self.assessment is None else self.assessment.reason_codes
        if self.outcome_status is OutcomeStatus.QUANTIFIED:
            if self.representative_value is None:
                raise ValueError("quantified outcome requires representative_value")
            if reason_codes:
                raise ValueError("reason_codes are only allowed for indeterminate outcome")
        elif self.outcome_status is OutcomeStatus.DIRECTIONAL:
            if self.representative_value is not None:
                raise ValueError("directional outcome must not include representative_value")
            if self.assessment is None:
                raise ValueError("directional outcome requires assessment")
            has_definite_trend = self.assessment.trend not in (
                None,
                TrendDirection.UNCERTAIN,
            )
            has_definite_risk = self.assessment.risk_level not in (
                None,
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
            if self.assessment is None or not reason_codes:
                raise ValueError(
                    "indeterminate outcome requires assessment and reason_codes"
                )
        return self
