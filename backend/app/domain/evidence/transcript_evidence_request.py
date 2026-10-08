from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory import MemoryMode
from app.domain.memory_rag import HistoricalSearchIntentV1


class TranscriptEvidenceRequest(DomainModel):
    operation_id: NonEmptyString
    run_id: NonEmptyString
    session_id: NonEmptyString
    memory_mode: MemoryMode
    intent: HistoricalSearchIntentV1
