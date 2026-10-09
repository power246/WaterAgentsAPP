"""M9 domain contract."""

from app.domain.domain_enum import DomainEnum


class AdminOperationState(DomainEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.INTERRUPTED}
