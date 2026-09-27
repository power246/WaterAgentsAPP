from typing import Protocol

from app.domain.hydrology import (
    StationAvailability,
    StationAvailabilityRequest,
)


class StationInventoryPort(Protocol):
    """Empty results mean no qualified station; unavailability is an error."""

    async def check_availability(
        self,
        request: StationAvailabilityRequest,
        *,
        deadline: float | None = None,
    ) -> tuple[StationAvailability, ...]: ...
