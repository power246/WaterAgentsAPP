from typing import Protocol

from app.domain.evidence import (
    DischargeObservationRecord,
    ProviderCollectionBatch,
    WaterObservationRecord,
    WaterObservationRequest,
)


class WaterObservationPort(Protocol):
    """Empty completed batches are distinct from provider unavailability."""

    async def collect(
        self,
        request: WaterObservationRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[WaterObservationRecord | DischargeObservationRecord]: ...
