from app.domain.common import DomainEnum


class TopologyCoverage(DomainEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"
