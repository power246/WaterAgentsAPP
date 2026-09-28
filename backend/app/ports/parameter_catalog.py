from typing import Protocol

from app.domain.hydrology import ParameterCatalogCandidate


class ParameterCatalogPort(Protocol):
    """Empty means no catalog match; provider unavailability must raise an error."""

    async def find_candidates(
        self,
        concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]: ...
