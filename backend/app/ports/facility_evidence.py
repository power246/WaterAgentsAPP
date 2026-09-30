from typing import Protocol

from app.domain.evidence import (
    FacilityEvidenceRequest,
    FacilityRecord,
    ProviderCollectionBatch,
)


class FacilityEvidencePort(Protocol):
    """Empty completed batches are distinct from provider unavailability."""

    async def collect(
        self,
        request: FacilityEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[FacilityRecord]: ...
