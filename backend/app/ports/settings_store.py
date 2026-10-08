from typing import Protocol
from uuid import UUID

from app.domain.settings_admin import Revision, SettingsSnapshot, UnitPolicyVersion, Version


class SettingsStorePort(Protocol):
    async def read_current(self) -> SettingsSnapshot | None: ...

    async def compare_and_swap(
        self, *, expected_revision: Revision, replacement: SettingsSnapshot,
    ) -> SettingsSnapshot: ...

    async def read_unit_policy(self, version: Version) -> UnitPolicyVersion | None: ...

    async def append_unit_policy(
        self, *, expected_revision: Revision, expected_unit_version: Version,
        policy: UnitPolicyVersion, replacement: SettingsSnapshot,
        idempotency_key: UUID | None = None, request_fingerprint: bytes | None = None,
        confirmation_token_digest: bytes | None = None,
    ) -> SettingsSnapshot: ...

    async def read_unit_policy_confirmation(
        self, idempotency_key: UUID,
    ) -> tuple[bytes, Version] | None: ...
