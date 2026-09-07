from typing import Protocol, TypeVar

RequestT = TypeVar("RequestT", contravariant=True)
RecordT = TypeVar("RecordT", covariant=True)


class WeatherObservationPort(Protocol[RequestT, RecordT]):
    """An empty tuple means a successful fetch had no records, not unavailable."""

    async def fetch(self, request: RequestT) -> tuple[RecordT, ...]: ...


class WeatherForecastPort(Protocol[RequestT, RecordT]):
    """An empty tuple means a successful fetch had no records, not unavailable."""

    async def fetch(self, request: RequestT) -> tuple[RecordT, ...]: ...
