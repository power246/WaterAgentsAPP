from typing import Protocol

from app.domain.evidence import (
    ProviderCollectionBatch,
    WeatherEvidenceRequest,
    WeatherForecastRecord,
    WeatherObservationRecord,
)


class WeatherEvidencePort(Protocol):
    """Empty completed batches are distinct from provider unavailability."""

    async def collect(
        self,
        request: WeatherEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[WeatherObservationRecord | WeatherForecastRecord]: ...
