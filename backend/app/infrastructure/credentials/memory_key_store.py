"""Explicitly injected key store for offline application tests."""

import asyncio

from app.domain.settings_admin import (
    CredentialName, CredentialStoreAvailability, StoredCredential,
)


class InMemoryKeyStore:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self._values: dict[CredentialName, StoredCredential] = {}
        self._lock = asyncio.Lock()

    async def availability(self) -> CredentialStoreAvailability:
        return (
            CredentialStoreAvailability.AVAILABLE if self.available
            else CredentialStoreAvailability.UNAVAILABLE
        )

    async def get(self, key_name: CredentialName) -> StoredCredential | None:
        self._require_available()
        async with self._lock:
            return self._values.get(key_name)

    async def set(self, key_name: CredentialName, value: StoredCredential) -> None:
        self._require_available()
        async with self._lock:
            self._values[key_name] = value

    async def delete(self, key_name: CredentialName) -> None:
        self._require_available()
        async with self._lock:
            self._values.pop(key_name, None)

    def _require_available(self) -> None:
        if not self.available:
            from app.application.settings_admin.errors import CredentialStoreUnavailableError

            raise CredentialStoreUnavailableError()


class OfflineCredentialTester:
    async def test(self, name: CredentialName, value) -> bool:
        del name, value
        return False
