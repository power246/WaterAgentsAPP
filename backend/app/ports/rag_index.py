from typing import Protocol, TypeVar

ItemT = TypeVar("ItemT", contravariant=True)
QueryT = TypeVar("QueryT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


class RAGIndexPort(Protocol[ItemT, QueryT, ResultT]):
    """An empty search result means no match, not index unavailability."""

    async def upsert(self, item: ItemT) -> None: ...

    async def search(self, query: QueryT) -> tuple[ResultT, ...]: ...
