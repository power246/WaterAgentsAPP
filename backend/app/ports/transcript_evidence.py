from typing import Protocol

from app.domain.evidence import (
    HistoricalTranscriptRecord,
    ProviderCollectionBatch,
    TranscriptEvidenceRequest,
)


class TranscriptEvidencePort(Protocol):
    """Empty completed batches are distinct from producer unavailability."""

    async def collect(
        self,
        request: TranscriptEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[HistoricalTranscriptRecord]: ...
