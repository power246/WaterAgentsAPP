from app.domain.common import DomainEnum


class RiskLevel(DomainEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INDETERMINATE = "indeterminate"
