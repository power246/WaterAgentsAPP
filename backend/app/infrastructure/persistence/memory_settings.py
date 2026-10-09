"""Settings store for explicitly injected, database-free test runtimes."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.application.settings_admin.errors import (
    SettingsRevisionConflictError, UnitPolicyRevisionConflictError,
)
from app.domain.hydrology import ParameterCapabilityRegistry
from app.domain.settings_admin import SettingsSnapshot, UnitPolicyEntry, UnitPolicyVersion


class InMemorySettingsRepository:
    def __init__(self, registry: ParameterCapabilityRegistry) -> None:
        self._lock = asyncio.Lock()
        self._current: SettingsSnapshot | None = None
        self._confirmations: dict[UUID, tuple[bytes, int]] = {}
        self._used_tokens: set[bytes] = set()
        self._versions = {1: UnitPolicyVersion(
            version=1,
            registry_digest=registry.manifest_digest,
            entries=tuple(sorted(
                (
                    UnitPolicyEntry(
                        parameter=capability.key,
                        target_unit_id=capability.unit_policy_id,
                    )
                    for capability in registry.capabilities
                ),
                key=lambda item: (item.parameter.code_system, item.parameter.code),
            )),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )}

    async def read_current(self) -> SettingsSnapshot | None:
        async with self._lock:
            return self._current

    async def read_unit_policy(self, version: int) -> UnitPolicyVersion | None:
        async with self._lock:
            return self._versions.get(version)

    async def compare_and_swap(
        self, *, expected_revision: int, replacement: SettingsSnapshot,
    ) -> SettingsSnapshot:
        async with self._lock:
            actual = self._current.revision if self._current is not None else 0
            if actual != expected_revision or replacement.revision != actual + 1:
                raise SettingsRevisionConflictError()
            policy = self._versions.get(replacement.unit_policy.version)
            if policy is None or policy.model_dump(exclude={"created_at"}) != replacement.unit_policy.model_dump():
                raise UnitPolicyRevisionConflictError()
            self._current = replacement
            return replacement

    async def append_unit_policy(
        self, *, expected_revision: int, expected_unit_version: int,
        policy: UnitPolicyVersion, replacement: SettingsSnapshot,
        idempotency_key: UUID | None = None, request_fingerprint: bytes | None = None,
        confirmation_token_digest: bytes | None = None,
    ) -> SettingsSnapshot:
        async with self._lock:
            actual = self._current.revision if self._current is not None else 0
            active = self._current.unit_policy.version if self._current is not None else 1
            if (
                actual != expected_revision or active != expected_unit_version
                or policy.version != active + 1 or policy.version in self._versions
                or replacement.revision != actual + 1
                or replacement.unit_policy.model_dump() != policy.model_dump(exclude={"created_at"})
                or (idempotency_key is not None and (
                    request_fingerprint is None or confirmation_token_digest is None
                    or idempotency_key in self._confirmations
                    or confirmation_token_digest in self._used_tokens
                ))
            ):
                raise UnitPolicyRevisionConflictError()
            self._versions[policy.version] = policy
            self._current = replacement
            if idempotency_key is not None:
                assert request_fingerprint is not None and confirmation_token_digest is not None
                self._confirmations[idempotency_key] = (request_fingerprint, policy.version)
                self._used_tokens.add(confirmation_token_digest)
            return replacement

    async def read_unit_policy_confirmation(
        self, idempotency_key: UUID,
    ) -> tuple[bytes, int] | None:
        async with self._lock:
            return self._confirmations.get(idempotency_key)
