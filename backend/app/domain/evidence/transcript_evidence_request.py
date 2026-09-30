from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory import MemoryMode


class TranscriptEvidenceRequest(DomainModel):
    operation_id: NonEmptyString
    run_id: NonEmptyString
    session_id: NonEmptyString
    memory_mode: MemoryMode
    query: NonEmptyString
    max_records: Annotated[int, Field(strict=True, ge=1, le=200)]
