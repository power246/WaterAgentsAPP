from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.memory_rag.embedding_profile import EmbeddingProfile
from app.domain.memory_rag.rag_index_generation_status import RAGIndexGenerationStatus


class RAGIndexGeneration(DomainModel):
    generation_id: NonEmptyString
    profile: EmbeddingProfile
    status: RAGIndexGenerationStatus
    source_cutoff_created_at: UtcDatetime
    source_cutoff_item_id: NonEmptyString
    created_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    retired_at: UtcDatetime | None = None
    failed_at: UtcDatetime | None = None
    last_safe_error_code: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status is RAGIndexGenerationStatus.BUILDING:
            if any((self.activated_at, self.retired_at, self.failed_at)):
                raise ValueError("building generation cannot have terminal timestamps")
        elif self.status is RAGIndexGenerationStatus.ACTIVE:
            if self.activated_at is None or self.retired_at or self.failed_at:
                raise ValueError("active generation requires only activated_at")
        elif self.status is RAGIndexGenerationStatus.RETIRED:
            if self.activated_at is None or self.retired_at is None or self.failed_at:
                raise ValueError("retired generation requires activation and retirement")
        else:
            if self.failed_at is None or self.activated_at or self.retired_at:
                raise ValueError("failed generation requires only failed_at")
            if self.last_safe_error_code is None:
                raise ValueError("failed generation requires a safe error code")
        return self
