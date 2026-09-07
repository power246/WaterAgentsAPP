from typing import Protocol, TypeVar

SecretT = TypeVar("SecretT")


class KeyStorePort(Protocol[SecretT]):
    """None means a key is absent; masked key status is intentionally deferred."""

    async def get(self, key_name: str) -> SecretT | None: ...

    async def set(self, key_name: str, value: SecretT) -> None: ...

    async def delete(self, key_name: str) -> None: ...
