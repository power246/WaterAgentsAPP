from app.domain.common import DomainEnum


class RiskLevel(DomainEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INDETERMINATE = "indeterminate"
