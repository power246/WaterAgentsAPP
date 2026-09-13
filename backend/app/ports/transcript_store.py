from typing import Protocol, TypeVar

ItemT = TypeVar("ItemT")


class TranscriptStorePort(Protocol[ItemT]):
    """An empty list result means the run has no items, not unavailable."""

    async def append(self, run_id: str, item: ItemT) -> None: ...

    async def list_for_run(self, run_id: str) -> tuple[ItemT, ...]: ...
