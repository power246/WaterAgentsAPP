from typing import Protocol

from app.domain.evidence import (
    ProviderCollectionBatch,
    UpstreamResultRecord,
    UpstreamResultRequest,
)


class UpstreamResultPort(Protocol):
    """Empty completed batches are distinct from producer unavailability."""

    async def collect(
        self,
        request: UpstreamResultRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[UpstreamResultRecord]: ...
