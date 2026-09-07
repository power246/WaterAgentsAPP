from typing import Protocol, TypeVar

ItemT = TypeVar("ItemT")


class TranscriptStorePort(Protocol[ItemT]):
    """An empty list result means the session has no items, not unavailable."""

    async def append(self, session_id: str, item: ItemT) -> None: ...

    async def list_for_session(self, session_id: str) -> tuple[ItemT, ...]: ...
