from typing import Protocol, TypeVar

RequestT = TypeVar("RequestT", contravariant=True)
RecordT = TypeVar("RecordT", covariant=True)


class FacilitySearchPort(Protocol[RequestT, RecordT]):
    """An empty tuple means a successful search had no records, not unavailable."""

    async def search(self, request: RequestT) -> tuple[RecordT, ...]: ...
