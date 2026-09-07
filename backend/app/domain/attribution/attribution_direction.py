from app.domain.common import DomainEnum


class AttributionDirection(DomainEnum):
    INCREASING = "increasing"
    DECREASING = "decreasing"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNKNOWN = "unknown"
