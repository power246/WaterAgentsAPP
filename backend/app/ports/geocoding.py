from typing import Protocol

from app.domain.hydrology import (
    AdministrativeLocation,
    GeocodingCandidate,
    GeoPoint,
)


class GeocodingPort(Protocol):
    """None or an empty tuple means no match; unavailability is an error."""

    async def resolve_text(
        self,
        location_text: str,
        *,
        deadline: float | None = None,
    ) -> tuple[GeocodingCandidate, ...]: ...

    async def reverse(
        self,
        point: GeoPoint,
        *,
        deadline: float | None = None,
    ) -> AdministrativeLocation | None: ...
