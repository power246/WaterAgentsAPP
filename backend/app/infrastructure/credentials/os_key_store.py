"""Versioned credential envelopes in an approved OS user keychain."""

import asyncio
import json
import sys
from datetime import datetime

from pydantic import SecretStr

from app.application.settings_admin.errors import CredentialStoreUnavailableError
from app.domain.settings_admin import (
    CredentialName, CredentialStoreAvailability, StoredCredential,
)

_SERVICE = "WaterAgentsApp.M9.user-credentials.v1"
_BACKENDS = {
    "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
    "darwin": ("keyring.backends.macOS", "Keyring"),
    "linux": ("keyring.backends.SecretService", "Keyring"),
}


class OSKeyStore:
    def __init__(self, *, backend=None, trusted_test_backend: bool = False) -> None:
        if backend is None:
            try:
                import keyring

                backend = keyring.get_keyring()
            except Exception:
                backend = None
        expected = _BACKENDS.get(sys.platform)
        self._backend = backend
        self._secure = bool(
            backend is not None
            and (trusted_test_backend or (
                expected is not None
                and (type(backend).__module__, type(backend).__name__) == expected
                and _positive_priority(backend)
            ))
        )

    async def availability(self) -> CredentialStoreAvailability:
        return (
            CredentialStoreAvailability.AVAILABLE
            if self._secure else CredentialStoreAvailability.UNAVAILABLE
        )

    async def get(self, key_name: CredentialName) -> StoredCredential | None:
        name = _account(key_name)
        backend = self._require_backend()
        try:
            encoded = await asyncio.to_thread(backend.get_password, _SERVICE, name)
            if encoded is None:
                return None
            envelope = json.loads(encoded)
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "value", "updated_at"}
                or type(envelope["schema_version"]) is not int
                or envelope["schema_version"] != 1
                or not isinstance(envelope["value"], str)
                or not isinstance(envelope["updated_at"], str)
            ):
                raise ValueError("invalid credential envelope")
            return StoredCredential(
                value=SecretStr(envelope["value"]),
                updated_at=datetime.fromisoformat(envelope["updated_at"]),
            )
        except Exception:
            pass
        raise CredentialStoreUnavailableError()

    async def set(self, key_name: CredentialName, value: StoredCredential) -> None:
        name = _account(key_name)
        backend = self._require_backend()
        encoded = json.dumps({
            "schema_version": 1,
            "value": value.value.get_secret_value(),
            "updated_at": value.updated_at.isoformat(),
        }, separators=(",", ":"), ensure_ascii=False)
        try:
            await asyncio.to_thread(backend.set_password, _SERVICE, name, encoded)
        except Exception:
            pass
        else:
            return
        raise CredentialStoreUnavailableError()

    async def delete(self, key_name: CredentialName) -> None:
        name = _account(key_name)
        backend = self._require_backend()
        try:
            if await asyncio.to_thread(backend.get_password, _SERVICE, name) is None:
                return
            await asyncio.to_thread(backend.delete_password, _SERVICE, name)
        except Exception:
            pass
        else:
            return
        raise CredentialStoreUnavailableError()

    def _require_backend(self):
        if not self._secure:
            raise CredentialStoreUnavailableError()
        return self._backend


def _account(key_name: CredentialName) -> str:
    if not isinstance(key_name, CredentialName):
        raise TypeError("credential identity must be allowlisted")
    return key_name.value


def _positive_priority(backend) -> bool:
    try:
        return backend.priority > 0
    except Exception:
        return False
