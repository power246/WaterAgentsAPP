from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.memory_rag.rag_index_job_status import RAGIndexJobStatus


class RAGIndexJob(DomainModel):
    job_id: NonEmptyString
    generation_id: NonEmptyString
    source_run_id: NonEmptyString
    source_session_id: NonEmptyString
    source_result_item_id: NonEmptyString
    projection_schema_version: Literal[1] = 1
    status: RAGIndexJobStatus
    attempts: Annotated[int, Field(strict=True, ge=0, le=5)]
    next_attempt_at: UtcDatetime | None = None
    lease_owner: NonEmptyString | None = None
    lease_until: UtcDatetime | None = None
    last_safe_error_code: NonEmptyString | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("job updated_at cannot precede created_at")
        leased = self.lease_owner is not None or self.lease_until is not None
        if self.status is RAGIndexJobStatus.LEASED:
            if self.lease_owner is None or self.lease_until is None:
                raise ValueError("leased job requires lease owner and deadline")
            if self.completed_at is not None:
                raise ValueError("leased job cannot be completed")
        elif leased:
            raise ValueError("non-leased job cannot retain lease fields")
        if self.status is RAGIndexJobStatus.RETRY_WAIT:
            if self.next_attempt_at is None or self.last_safe_error_code is None:
                raise ValueError("retry job requires next attempt and safe error code")
        elif self.next_attempt_at is not None:
            raise ValueError("only retry job may declare next_attempt_at")
        terminal = self.status in {
            RAGIndexJobStatus.COMPLETED,
            RAGIndexJobStatus.EXCLUDED,
            RAGIndexJobStatus.FAILED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal job status must match completed_at")
        if self.status in {RAGIndexJobStatus.EXCLUDED, RAGIndexJobStatus.FAILED}:
            if self.last_safe_error_code is None:
                raise ValueError("excluded or failed job requires a safe error code")
        if self.status is RAGIndexJobStatus.COMPLETED and self.last_safe_error_code:
            raise ValueError("completed job cannot retain a safe error code")
        return self
