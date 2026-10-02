from typing import Protocol

from app.domain.hydrology import (
    GeoPoint,
    MonitoringStation,
    RiverSegment,
)


class ComidLookupPort(Protocol):
    """None means a successful lookup found no COMID; unavailability is an error."""

    async def lookup_position(
        self,
        point: GeoPoint,
        *,
        deadline: float | None = None,
    ) -> str | None: ...


class TopologyDataPort(Protocol):
    """None or an empty tuple means no match; unavailability is a port error."""

    async def get_segment(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> RiverSegment | None: ...

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
        *,
        deadline: float | None = None,
    ) -> tuple[RiverSegment, ...]: ...

    async def get_monitoring_sites(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> tuple[MonitoringStation, ...]: ...
