from typing import Protocol

from app.domain.settings_admin import Revision, SettingsSnapshot, UnitPolicyVersion, Version


class SettingsStorePort(Protocol):
    async def read_current(self) -> SettingsSnapshot | None: ...

    async def compare_and_swap(
        self, *, expected_revision: Revision, replacement: SettingsSnapshot,
    ) -> SettingsSnapshot: ...

    async def read_unit_policy(self, version: Version) -> UnitPolicyVersion | None: ...

    async def append_unit_policy(
        self, *, expected_revision: Revision, expected_unit_version: Version,
        policy: UnitPolicyVersion,
    ) -> SettingsSnapshot: ...
