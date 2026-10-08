"""Preview and atomically confirm append-only target unit policies."""

import json
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID

from app.application.settings_admin.confirmation_registry import ConfirmationRegistry
from app.application.settings_admin.contracts import validate_unit_policy
from app.application.settings_admin.errors import (
    AdminConfirmationInvalidError, AdminPreviewStaleError,
    UnitPolicyIdempotencyConflictError, UnitPolicyRevisionConflictError,
    UnitUnknownError,
)
from app.application.settings_admin.settings_service import SettingsService
from app.domain.hydrology import ParameterCapabilityRegistry
from app.domain.settings_admin import (
    AdminConfirmation, SettingsSnapshot, UnitPolicyChange,
    UnitPolicyEntry, UnitPolicyPreview, UnitPolicyProjection, UnitPolicyVersion,
)
from app.ports.clock import ClockPort
from app.ports.settings_store import SettingsStorePort


def _fingerprint(snapshot: SettingsSnapshot, proposed: UnitPolicyProjection) -> bytes:
    data = {
        "revision": snapshot.revision,
        "base_version": snapshot.unit_policy.version,
        "current": snapshot.unit_policy.model_dump(mode="json"),
        "proposed": proposed.model_dump(mode="json"),
    }
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).digest()


@dataclass(frozen=True, slots=True)
class UnitPolicyPreviewResult:
    preview: UnitPolicyPreview
    affected_dimensions: tuple[str, ...]
    confirmation: AdminConfirmation | None


@dataclass(frozen=True, slots=True)
class _PendingPolicy:
    preview: UnitPolicyPreview
    base_revision: int
    facts: bytes


class UnitPolicyService:
    def __init__(
        self, *, settings: SettingsService, store: SettingsStorePort,
        registry: ParameterCapabilityRegistry, clock: ClockPort,
        confirmations: ConfirmationRegistry[_PendingPolicy] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._registry = registry
        self._clock = clock
        self._confirmations = confirmations or ConfirmationRegistry()

    async def preview(
        self, *, base_version: int, entries: tuple[UnitPolicyEntry, ...],
    ) -> UnitPolicyPreviewResult:
        current = await self._settings.get_snapshot()
        if current.unit_policy.version != base_version:
            raise UnitPolicyRevisionConflictError()
        if len({entry.parameter for entry in entries}) != len(entries):
            raise UnitUnknownError()
        current_entries = {entry.parameter: entry for entry in current.unit_policy.entries}
        if not set(entry.parameter for entry in entries) <= set(current_entries):
            raise UnitUnknownError()
        current_entries.update({entry.parameter: entry for entry in entries})
        normalized = tuple(sorted(
            current_entries.values(),
            key=lambda entry: (entry.parameter.code_system, entry.parameter.code),
        ))
        proposed = UnitPolicyProjection(
            version=base_version + 1,
            registry_digest=self._registry.manifest_digest,
            entries=normalized,
        )
        validate_unit_policy(proposed, self._registry)
        before = {entry.parameter: entry.target_unit_id for entry in current.unit_policy.entries}
        changes = tuple(UnitPolicyChange(
            parameter=entry.parameter,
            previous_unit_id=before.get(entry.parameter),
            target_unit_id=entry.target_unit_id,
            dimension_id=self._registry.unit_definitions[entry.target_unit_id].dimension_id,
        ) for entry in normalized if before.get(entry.parameter) != entry.target_unit_id)
        preview = UnitPolicyPreview(
            base_version=base_version, proposed=proposed, changes=changes,
        )
        confirmation = None
        if changes:
            confirmation = await self._confirmations.issue(
                action="unit_policy",
                phrase=f"APPLY UNIT POLICY {proposed.version}",
                payload=_PendingPolicy(preview, current.revision, _fingerprint(current, proposed)),
                now=self._clock.now(),
            )
        return UnitPolicyPreviewResult(
            preview=preview,
            affected_dimensions=tuple(sorted({change.dimension_id for change in changes})),
            confirmation=confirmation,
        )

    async def confirm(
        self, *, token: str, phrase: str, idempotency_key: UUID,
        raw_body: bytes,
    ) -> UnitPolicyVersion:
        request_fingerprint = sha256(raw_body).digest()
        receipt = await self._store.read_unit_policy_confirmation(idempotency_key)
        if receipt is not None:
            if not compare_digest(receipt[0], request_fingerprint):
                raise UnitPolicyIdempotencyConflictError()
            saved = await self._store.read_unit_policy(receipt[1])
            if saved is None:
                raise AdminPreviewStaleError()
            return saved
        try:
            async with self._confirmations.claim(
                token=token, action="unit_policy",
                phrase=phrase, now=self._clock.now(),
            ) as (pending, token_digest):
                current = await self._settings.get_snapshot()
                proposed = pending.preview.proposed
                if (
                    current.revision != pending.base_revision
                    or current.unit_policy.version != pending.preview.base_version
                    or not compare_digest(_fingerprint(current, proposed), pending.facts)
                ):
                    raise AdminPreviewStaleError()
                validate_unit_policy(proposed, self._registry)
                policy = UnitPolicyVersion(
                    **proposed.model_dump(), created_at=self._clock.now(),
                )
                replacement = SettingsSnapshot(
                    revision=current.revision + 1,
                    defaults=current.defaults,
                    unit_policy=proposed,
                    updated_at=self._clock.now(),
                )
                await self._store.append_unit_policy(
                    expected_revision=current.revision,
                    expected_unit_version=current.unit_policy.version,
                    policy=policy, replacement=replacement,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    confirmation_token_digest=token_digest,
                )
                return policy
        except (
            AdminConfirmationInvalidError,
            AdminPreviewStaleError,
            UnitPolicyRevisionConflictError,
        ):
            receipt = await self._store.read_unit_policy_confirmation(idempotency_key)
            if receipt is None:
                raise
            if not compare_digest(receipt[0], request_fingerprint):
                raise UnitPolicyIdempotencyConflictError() from None
            saved = await self._store.read_unit_policy(receipt[1])
            if saved is None:
                raise AdminPreviewStaleError() from None
            return saved
