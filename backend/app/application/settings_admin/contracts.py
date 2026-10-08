"""Catalog and public credential projections for the current M9 V1 contract."""

from datetime import datetime

from pydantic import SecretStr

from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.domain.hydrology.unit_kind import UnitKind
from app.domain.model_catalog_snapshot import ModelCatalogSnapshot
from app.domain.model_role import ModelRole
from app.domain.settings_admin import (
    CredentialName, CredentialSource, CredentialStatus, EmbeddingProfileOption,
    ModelOption, MutableDefaults, SettingsCatalogProjection, UnitPolicyEntry,
    UnitPolicyProjection,
)
from app.domain.time.time_resolution_preference import TimeResolutionPreference
from app.domain.memory import MemoryMode
from app.application.settings_admin.errors import (
    UnitDimensionMismatchError, UnitNotApplicableError,
    UnitPolicyRevisionConflictError, UnitUnknownError,
)


_CREDENTIAL_METADATA = {
    CredentialName.OPENAI: (
        "OpenAI", ("query_parser", "waterbody", "run_summary", "embedding"),
    ),
    CredentialName.GOOGLE_GEOCODING: ("Google Geocoding", ("geocoding",)),
}


def safe_settings_catalog(
    catalog: ModelCatalogSnapshot, *, iana_timezones: tuple[str, ...],
) -> SettingsCatalogProjection:
    options = tuple(
        ModelOption(
            selection={"provider": item.provider, "model": item.model},
            label=f"{item.provider} / {item.model}",
            roles=tuple(role for role in item.roles if role is not ModelRole.EMBEDDING),
            available=True,
        )
        for item in catalog.capabilities
        if ModelRole.QUERY_PARSER in item.roles or ModelRole.WATERBODY in item.roles
    )
    embedding = tuple(
        EmbeddingProfileOption(
            profile_id=item.embedding_profile.profile_id,
            label=f"{item.provider} / {item.model}",
            available=True,
        )
        for item in catalog.capabilities
        if item.embedding_profile is not None
    )
    return SettingsCatalogProjection(
        models=options,
        embedding_profiles=embedding,
        memory_modes=tuple(MemoryMode),
        time_resolutions=tuple(TimeResolutionPreference),
        iana_timezones=iana_timezones,
    )


def validate_mutable_defaults(
    defaults: MutableDefaults, catalog: ModelCatalogSnapshot,
) -> None:
    for field_name, role in (
        ("parser_model", ModelRole.QUERY_PARSER),
        ("waterbody_model", ModelRole.WATERBODY),
    ):
        if not catalog.supports(getattr(defaults, field_name), role):
            raise ValueError(f"{field_name} is not selectable for {role.value}")
    registered_profiles = {
        item.embedding_profile.profile_id
        for item in catalog.capabilities if item.embedding_profile is not None
    }
    if defaults.desired_embedding_profile_id not in registered_profiles:
        raise ValueError("desired_embedding_profile_id is not registered")


def validate_unit_policy(
    policy: UnitPolicyProjection, registry: ParameterCapabilityRegistry,
) -> None:
    if policy.registry_digest != registry.manifest_digest:
        raise UnitPolicyRevisionConflictError()
    for entry in policy.entries:
        capability = registry.get(entry.parameter)
        if capability is None:
            raise UnitUnknownError()
        source_unit = registry.unit_definitions[capability.unit_policy_id]
        target_unit = registry.unit_definitions.get(entry.target_unit_id)
        if target_unit is None:
            raise UnitUnknownError()
        if source_unit.dimension_id != target_unit.dimension_id:
            raise UnitDimensionMismatchError()
        if (
            source_unit.kind is UnitKind.IDENTITY_ONLY
            or target_unit.kind is UnitKind.IDENTITY_ONLY
        ) and source_unit.unit_id != target_unit.unit_id:
            raise UnitNotApplicableError()


def project_credential_status(
    name: CredentialName, *, source: CredentialSource, value: SecretStr | None,
    updated_at: datetime | None, store_available: bool,
) -> CredentialStatus:
    if (source is CredentialSource.NONE) != (value is None):
        raise ValueError("effective credential source and value disagree")
    if source is CredentialSource.USER_STORE and updated_at is None:
        raise ValueError("user-store credential requires updated_at")
    if source is not CredentialSource.USER_STORE and updated_at is not None:
        raise ValueError("only user-store credential has updated_at")
    secret = value.get_secret_value() if value is not None else None
    if secret is not None and not secret.strip():
        raise ValueError("effective credential is empty")
    label, purpose = _CREDENTIAL_METADATA[name]
    return CredentialStatus(
        key_name=name,
        label=label,
        purpose=purpose,
        configured=secret is not None,
        source=source,
        masked=("••••" + secret[-4:] if len(secret) >= 8 else "••••")
        if secret is not None else None,
        updated_at=updated_at,
        store_available=store_available,
        can_save=store_available,
        can_delete_user_override=(
            store_available and source is CredentialSource.USER_STORE
        ),
        can_test=secret is not None,
    )
