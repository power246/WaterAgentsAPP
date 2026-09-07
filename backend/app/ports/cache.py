from typing import Protocol, TypeVar

ValueT = TypeVar("ValueT")


class CachePort(Protocol[ValueT]):
    """None means a cache miss; backend unavailability is a port error."""

    async def get(self, namespace: str, key: str) -> ValueT | None: ...

    async def set(self, namespace: str, key: str, value: ValueT) -> None: ...

    async def delete(self, namespace: str, key: str) -> None: ...

    async def clear_namespace(self, namespace: str) -> None: ...
