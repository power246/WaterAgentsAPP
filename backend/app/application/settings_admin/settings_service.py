"""Safe mutable defaults and immutable per-request settings snapshots."""

from zoneinfo import available_timezones

from app.application.settings_admin.contracts import (
    safe_settings_catalog, validate_mutable_defaults,
)
from app.application.settings_admin.errors import (
    SettingsInvalidDefaultsError, SettingsRevisionConflictError,
    SettingsUnavailableError,
)
from app.domain.memory import MemoryMode
from app.domain.model_catalog_snapshot import ModelCatalogSnapshot
from app.domain.model_role import ModelRole
from app.domain.settings_admin import (
    MutableDefaults, SettingsCatalogProjection, SettingsSnapshot,
)
from app.domain.time.time_resolution_preference import TimeResolutionPreference
from app.ports.clock import ClockPort
from app.ports.settings_store import SettingsStorePort


def deployment_defaults(
    analysis_timezone: str, catalog: ModelCatalogSnapshot,
) -> MutableDefaults:
    try:
        parser = next(item for item in catalog.capabilities if ModelRole.QUERY_PARSER in item.roles)
        waterbody = next(item for item in catalog.capabilities if ModelRole.WATERBODY in item.roles)
        profile = catalog.embedding_profile
    except StopIteration as error:
        raise ValueError("deployment model catalog lacks a required role") from error
    defaults = MutableDefaults(
        analysis_timezone=analysis_timezone,
        parser_model={"provider": parser.provider, "model": parser.model},
        waterbody_model={"provider": waterbody.provider, "model": waterbody.model},
        memory_mode=MemoryMode.STATELESS,
        time_resolution=TimeResolutionPreference.TWELVE_HOURS,
        desired_embedding_profile_id=profile.profile_id,
    )
    validate_mutable_defaults(defaults, catalog)
    return defaults


class SettingsService:
    def __init__(
        self, store: SettingsStorePort, catalog: ModelCatalogSnapshot,
        defaults: MutableDefaults, clock: ClockPort,
    ) -> None:
        validate_mutable_defaults(defaults, catalog)
        self._store = store
        self._model_catalog = catalog
        self._defaults = defaults
        self._clock = clock
        self.catalog: SettingsCatalogProjection = safe_settings_catalog(
            catalog, iana_timezones=tuple(sorted(available_timezones())),
        )

    async def get_snapshot(self) -> SettingsSnapshot:
        saved = await self._store.read_current()
        if saved is not None:
            return saved
        policy = await self._store.read_unit_policy(1)
        if policy is None:
            raise SettingsUnavailableError()
        return SettingsSnapshot(
            revision=0, defaults=self._defaults,
            unit_policy=policy.model_dump(exclude={"created_at"}),
            updated_at=None,
        )

    async def replace(
        self, *, expected_revision: int, defaults: MutableDefaults,
    ) -> SettingsSnapshot:
        try:
            validate_mutable_defaults(defaults, self._model_catalog)
        except ValueError as error:
            raise SettingsInvalidDefaultsError() from error
        current = await self.get_snapshot()
        if current.revision != expected_revision:
            raise SettingsRevisionConflictError(current)
        replacement = SettingsSnapshot(
            revision=expected_revision + 1,
            defaults=defaults,
            unit_policy=current.unit_policy,
            updated_at=self._clock.now(),
        )
        try:
            return await self._store.compare_and_swap(
                expected_revision=expected_revision, replacement=replacement,
            )
        except SettingsRevisionConflictError as error:
            raise SettingsRevisionConflictError(await self.get_snapshot()) from error
