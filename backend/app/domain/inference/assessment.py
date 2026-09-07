from app.domain.common import DomainModel, NonEmptyString
from app.domain.inference.indeterminate_reason import IndeterminateReason
from app.domain.inference.risk_level import RiskLevel
from app.domain.inference.trend_direction import TrendDirection


class RiskOrTrendAssessment(DomainModel):
    summary: NonEmptyString
    trend: TrendDirection | None
    risk_level: RiskLevel | None
    reason_codes: tuple[IndeterminateReason, ...] = ()
