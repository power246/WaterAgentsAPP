from app.domain.domain_enum import DomainEnum


class EvidenceToolExecutionStatus(DomainEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
