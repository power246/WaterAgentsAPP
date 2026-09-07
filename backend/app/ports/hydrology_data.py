from typing import Protocol, TypeVar

from app.domain.hydrology import (
    GeoPoint,
    MonitoringStation,
    ParameterKey,
    RiverSegment,
)

RequestT = TypeVar("RequestT", contravariant=True)
RecordT = TypeVar("RecordT", covariant=True)


class ComidLookupPort(Protocol):
    """None means a successful lookup found no COMID; unavailability is an error."""

    async def lookup_comid(self, point: GeoPoint) -> str | None: ...


class TopologyDataPort(Protocol):
    """None or an empty tuple means no match; unavailability is a port error."""

    async def get_segment(self, comid: str) -> RiverSegment | None: ...

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
    ) -> tuple[RiverSegment, ...]: ...


class MonitoringStationPort(Protocol):
    """An empty tuple means no station matched; unavailability is a port error."""

    async def find_for_segment(
        self,
        comid: str,
        parameters: tuple[ParameterKey, ...],
    ) -> tuple[MonitoringStation, ...]: ...


class WaterObservationPort(Protocol[RequestT, RecordT]):
    """An empty tuple means a successful fetch had no records, not unavailable."""

    async def fetch(self, request: RequestT) -> tuple[RecordT, ...]: ...
