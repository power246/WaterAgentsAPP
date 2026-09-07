from app.domain.common import DomainEnum


class TrendDirection(DomainEnum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"
    UNCERTAIN = "uncertain"
