from pydantic import model_validator

from app.domain.common import (
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.inference.indeterminate_reason import IndeterminateReason
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection


class RiskOrTrendAssessment(DomainModel):
    summary: NonEmptyString
    trend: TrendDirection | None
    risk_level: RiskLevel
    risk_rationale: NonEmptyString
    reason_codes: tuple[IndeterminateReason, ...] = ()
    supporting_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_references(self) -> "RiskOrTrendAssessment":
        ensure_unique_tuple(self.reason_codes, field_name="reason_codes")
        if not self.supporting_refs:
            raise ValueError("supporting_refs must not be empty")
        ensure_unique_tuple(self.supporting_refs, field_name="supporting_refs")
        return self
