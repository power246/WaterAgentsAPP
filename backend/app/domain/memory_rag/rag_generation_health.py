from typing import Annotated

from pydantic import Field, StrictInt

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.memory_rag.rag_index_generation_status import (
    RAGIndexGenerationStatus,
)
from app.domain.memory_rag.rag_job_counts import RAGJobCounts


class RAGGenerationHealth(DomainModel):
    generation_id: NonEmptyString
    status: RAGIndexGenerationStatus
    profile_digest: NonEmptyString
    job_counts: RAGJobCounts
    reader_count: Annotated[StrictInt, Field(ge=0)]
    created_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    retired_at: UtcDatetime | None = None
    failed_at: UtcDatetime | None = None
    last_safe_error_code: NonEmptyString | None = None
