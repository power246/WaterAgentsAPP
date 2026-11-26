from app.domain.domain_enum import DomainEnum


class GuidelineOperationState(DomainEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"
