from app.domain.common import DomainEnum


class RAGIndexJobStatus(DomainEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    EXCLUDED = "excluded"
    FAILED = "failed"
