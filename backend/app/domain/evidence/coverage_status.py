from app.domain.domain_enum import DomainEnum


class CoverageStatus(DomainEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"
