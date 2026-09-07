from typing import Protocol

from app.domain.hydrology import GeoPoint


class GeocodingPort(Protocol):
    """None means a successful lookup found no match; unavailability is an error."""

    async def resolve(self, location_text: str) -> GeoPoint | None: ...
