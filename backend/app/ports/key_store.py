from typing import Protocol

from app.domain.settings_admin import (
    CredentialName, CredentialStoreAvailability, StoredCredential,
)


class KeyStorePort(Protocol):
    """Typed OS user-store envelope; None means no user override."""

    async def availability(self) -> CredentialStoreAvailability: ...

    async def get(self, key_name: CredentialName) -> StoredCredential | None: ...

    async def set(self, key_name: CredentialName, value: StoredCredential) -> None: ...

    async def delete(self, key_name: CredentialName) -> None: ...
