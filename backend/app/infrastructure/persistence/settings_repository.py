"""Transactional singleton settings and append-only unit policy persistence."""

import asyncio

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.application.settings_admin.contracts import validate_unit_policy
from app.application.settings_admin.errors import (
    SettingsRevisionConflictError, SettingsUnavailableError,
    UnitPolicyRevisionConflictError,
)
from app.domain.hydrology import ParameterCapabilityRegistry
from app.domain.settings_admin import (
    SettingsSnapshot, UnitPolicyEntry, UnitPolicyProjection, UnitPolicyVersion,
)
from app.infrastructure.persistence.schema import (
    runtime_settings, unit_policy_entries, unit_policy_versions,
)


class PostgresSettingsRepository:
    def __init__(
        self, engine: AsyncEngine, registry: ParameterCapabilityRegistry, *,
        operation_timeout_seconds: float,
    ) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._registry = registry
        self._timeout = operation_timeout_seconds

    async def read_current(self) -> SettingsSnapshot | None:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    row = (await connection.execute(select(runtime_settings))).mappings().one_or_none()
                    if row is None:
                        return None
                    policy = await self._read_policy(connection, row["active_unit_policy_version"])
                    return self._snapshot(row, policy)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise SettingsUnavailableError() from error

    async def read_unit_policy(self, version: int) -> UnitPolicyVersion | None:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    return await self._read_policy(connection, version)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise SettingsUnavailableError() from error

    async def compare_and_swap(
        self, *, expected_revision: int, replacement: SettingsSnapshot,
    ) -> SettingsSnapshot:
        if replacement.revision != expected_revision + 1:
            raise SettingsRevisionConflictError()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    current = (await connection.execute(
                        select(runtime_settings.c.revision, runtime_settings.c.active_unit_policy_version)
                        .where(runtime_settings.c.singleton_id == 1).with_for_update()
                    )).mappings().one_or_none()
                    if current is None:
                        if expected_revision != 0 or replacement.unit_policy.version != 1:
                            raise SettingsRevisionConflictError()
                        await self._insert_settings(connection, replacement)
                    else:
                        if (
                            current["revision"] != expected_revision
                            or current["active_unit_policy_version"] != replacement.unit_policy.version
                        ):
                            raise SettingsRevisionConflictError()
                        await self._update_settings(
                            connection, replacement, expected_revision=expected_revision,
                        )
                    actual = await self._read_policy(connection, replacement.unit_policy.version)
                    if actual != self._as_version(replacement.unit_policy, actual):
                        raise UnitPolicyRevisionConflictError()
                    return replacement
        except IntegrityError as error:
            raise SettingsRevisionConflictError() from error
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise SettingsUnavailableError() from error

    async def append_unit_policy(
        self, *, expected_revision: int, expected_unit_version: int,
        policy: UnitPolicyVersion, replacement: SettingsSnapshot,
    ) -> SettingsSnapshot:
        if (
            policy.version != expected_unit_version + 1
            or replacement.revision != expected_revision + 1
            or replacement.unit_policy.version != policy.version
            or replacement.unit_policy.registry_digest != policy.registry_digest
            or replacement.unit_policy.entries != policy.entries
        ):
            raise UnitPolicyRevisionConflictError()
        validate_unit_policy(policy, self._registry)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    current = (await connection.execute(
                        select(runtime_settings.c.revision, runtime_settings.c.active_unit_policy_version)
                        .where(runtime_settings.c.singleton_id == 1).with_for_update()
                    )).mappings().one_or_none()
                    if current is None:
                        if expected_revision != 0 or expected_unit_version != 1:
                            raise UnitPolicyRevisionConflictError()
                    elif (
                        current["revision"] != expected_revision
                        or current["active_unit_policy_version"] != expected_unit_version
                    ):
                        raise UnitPolicyRevisionConflictError()
                    if await self._read_policy(connection, expected_unit_version) is None:
                        raise UnitPolicyRevisionConflictError()
                    await connection.execute(insert(unit_policy_versions).values(
                        version=policy.version, registry_digest=policy.registry_digest,
                        created_at=policy.created_at,
                    ))
                    if policy.entries:
                        await connection.execute(insert(unit_policy_entries), [
                            {
                                "version": policy.version,
                                "parameter_code_system": item.parameter.code_system,
                                "parameter_code": item.parameter.code,
                                "target_unit_id": item.target_unit_id,
                            }
                            for item in policy.entries
                        ])
                    await connection.execute(update(unit_policy_versions).where(
                        unit_policy_versions.c.version == policy.version
                    ).values(sealed=True))
                    if current is None:
                        await self._insert_settings(connection, replacement)
                    else:
                        await self._update_settings(
                            connection, replacement, expected_revision=expected_revision,
                        )
                    return replacement
        except IntegrityError as error:
            raise UnitPolicyRevisionConflictError() from error
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise SettingsUnavailableError() from error

    async def _read_policy(
        self, connection: AsyncConnection, version: int,
    ) -> UnitPolicyVersion | None:
        row = (await connection.execute(
            select(unit_policy_versions).where(unit_policy_versions.c.version == version)
        )).mappings().one_or_none()
        if row is None:
            return None
        if not row["sealed"]:
            raise UnitPolicyRevisionConflictError()
        entries = tuple(UnitPolicyEntry(
            parameter={"code_system": entry["parameter_code_system"],
                       "code": entry["parameter_code"]},
            target_unit_id=entry["target_unit_id"],
        ) for entry in (await connection.execute(
            select(unit_policy_entries)
            .where(unit_policy_entries.c.version == version)
            .order_by(unit_policy_entries.c.parameter_code_system,
                      unit_policy_entries.c.parameter_code)
        )).mappings())
        policy = UnitPolicyVersion(
            version=row["version"], registry_digest=row["registry_digest"],
            entries=entries, created_at=row["created_at"],
        )
        validate_unit_policy(policy, self._registry)
        return policy

    @staticmethod
    def _snapshot(row, policy: UnitPolicyVersion) -> SettingsSnapshot:
        return SettingsSnapshot.model_validate({
            "revision": row["revision"], "updated_at": row["updated_at"],
            "unit_policy": policy.model_dump(exclude={"created_at"}),
            "defaults": {
                "analysis_timezone": row["analysis_timezone"],
                "parser_model": {"provider": row["parser_provider"],
                                 "model": row["parser_model"]},
                "waterbody_model": {"provider": row["waterbody_provider"],
                                    "model": row["waterbody_model"]},
                "memory_mode": row["memory_mode"],
                "time_resolution": row["time_resolution"],
                "desired_embedding_profile_id": row["desired_embedding_profile_id"],
            },
        })

    @staticmethod
    def _settings_values(snapshot: SettingsSnapshot) -> dict[str, object]:
        defaults = snapshot.defaults
        return {
            "singleton_id": 1, "schema_version": 1,
            "revision": snapshot.revision,
            "analysis_timezone": defaults.analysis_timezone,
            "parser_provider": defaults.parser_model.provider,
            "parser_model": defaults.parser_model.model,
            "waterbody_provider": defaults.waterbody_model.provider,
            "waterbody_model": defaults.waterbody_model.model,
            "memory_mode": defaults.memory_mode.value,
            "time_resolution": defaults.time_resolution.value,
            "desired_embedding_profile_id": defaults.desired_embedding_profile_id,
            "active_unit_policy_version": snapshot.unit_policy.version,
            "updated_at": snapshot.updated_at,
        }

    async def _insert_settings(
        self, connection: AsyncConnection, replacement: SettingsSnapshot,
    ) -> None:
        await connection.execute(insert(runtime_settings).values(**self._settings_values(replacement)))

    async def _update_settings(
        self, connection: AsyncConnection, replacement: SettingsSnapshot, *,
        expected_revision: int,
    ) -> None:
        result = await connection.execute(
            update(runtime_settings).where(
                runtime_settings.c.singleton_id == 1,
                runtime_settings.c.revision == expected_revision,
            )
            .values(**self._settings_values(replacement))
        )
        if result.rowcount != 1:
            raise SettingsRevisionConflictError()

    @staticmethod
    def _as_version(
        projection: UnitPolicyProjection, actual: UnitPolicyVersion,
    ) -> UnitPolicyVersion:
        return UnitPolicyVersion(
            **projection.model_dump(), created_at=actual.created_at,
        )
