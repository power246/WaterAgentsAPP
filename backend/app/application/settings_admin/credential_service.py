"""Allowlisted effective credentials and immutable run/attempt leases."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from app.application.settings_admin.contracts import project_credential_status
from app.application.settings_admin.errors import (
    CredentialMissingError, CredentialStoreUnavailableError,
    CredentialTestFailedError, CredentialValueInvalidError,
)
from app.domain.settings_admin import (
    AdminSafeError, CredentialLease, CredentialName, CredentialSource,
    CredentialStatus, CredentialStoreAvailability, StoredCredential,
)
from app.domain.common import UtcDatetime
from app.ports.clock import ClockPort
from app.ports.key_store import KeyStorePort
from app.application.transcripts.redaction import register_private_value


class CredentialTester(Protocol):
    async def test(self, name: CredentialName, value: SecretStr) -> bool: ...


@dataclass(frozen=True, slots=True)
class CredentialTestResult:
    key_name: CredentialName
    succeeded: bool
    safe_error: AdminSafeError | None
    checked_at: UtcDatetime


class CredentialService:
    def __init__(
        self, store: KeyStorePort, deployment: dict[CredentialName, SecretStr | None],
        clock: ClockPort, tester: CredentialTester,
        *, embedding_uses_openai: bool = True,
    ) -> None:
        if set(deployment) != set(CredentialName):
            raise ValueError("deployment credential mapping must match the allowlist")
        self._store = store
        self._deployment = dict(deployment)
        for value in deployment.values():
            if value is not None:
                register_private_value(value.get_secret_value())
        self._clock = clock
        self._tester = tester
        self._embedding_uses_openai = embedding_uses_openai

    async def statuses(self) -> tuple[CredentialStatus, ...]:
        return tuple([await self.status(name) for name in CredentialName])

    async def status(self, name: CredentialName) -> CredentialStatus:
        stored, available = await self._read_user(name)
        if stored is not None:
            return project_credential_status(
                name, source=CredentialSource.USER_STORE,
                value=stored.value, updated_at=stored.updated_at,
                store_available=available,
                embedding_uses_openai=self._embedding_uses_openai,
            )
        deployment = self._deployment[name]
        return project_credential_status(
            name, source=(CredentialSource.DEPLOYMENT if deployment is not None
                         else CredentialSource.NONE),
            value=deployment, updated_at=None, store_available=available,
            embedding_uses_openai=self._embedding_uses_openai,
        )

    async def lease(self, name: CredentialName) -> CredentialLease:
        stored, available = await self._read_user(name)
        if stored is not None:
            register_private_value(stored.value.get_secret_value())
            value, source = stored.value, CredentialSource.USER_STORE
        else:
            value, source = self._deployment[name], CredentialSource.DEPLOYMENT
        if value is None:
            if not available:
                raise CredentialStoreUnavailableError()
            raise CredentialMissingError()
        register_private_value(value.get_secret_value())
        return CredentialLease(
            name=name, value=value, source=source, issued_at=self._clock.now(),
        )

    async def save(self, name: CredentialName, value: SecretStr) -> CredentialStatus:
        raw = value.get_secret_value()
        if not raw.strip() or "\x00" in raw or len(raw) > 4096:
            raise CredentialValueInvalidError()
        register_private_value(raw)
        await self._require_available()
        await self._store.set(name, StoredCredential(value, self._clock.now()))
        return await self.status(name)

    async def delete(self, name: CredentialName) -> CredentialStatus:
        await self._require_available()
        await self._store.delete(name)
        return await self.status(name)

    async def test(self, name: CredentialName) -> CredentialTestResult:
        lease = await self.lease(name)
        try:
            succeeded = await self._tester.test(name, lease.value)
        except Exception:
            pass
        else:
            return CredentialTestResult(
                key_name=name,
                succeeded=succeeded,
                safe_error=None if succeeded else AdminSafeError(
                    code="credential_rejected",
                    message="The provider did not accept this credential.",
                    retryable=False,
                ),
                checked_at=self._clock.now(),
            )
        raise CredentialTestFailedError()

    async def _read_user(self, name: CredentialName) -> tuple[StoredCredential | None, bool]:
        try:
            available = await self._store.availability()
            if available is CredentialStoreAvailability.AVAILABLE:
                stored = await self._store.get(name)
                if stored is not None:
                    register_private_value(stored.value.get_secret_value())
                return stored, True
        except CredentialStoreUnavailableError:
            pass
        return None, False

    async def _require_available(self) -> None:
        if await self._store.availability() is not CredentialStoreAvailability.AVAILABLE:
            raise CredentialStoreUnavailableError()
