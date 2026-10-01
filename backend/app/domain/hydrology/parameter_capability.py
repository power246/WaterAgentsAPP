from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.channel_aggregation_default import (
    ChannelAggregationDefault,
)
from app.domain.hydrology.channel_policy import ChannelPolicy
from app.domain.hydrology.parameter_concept_normalization import (
    normalize_parameter_concept,
)
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.parameter_role import ParameterRole


class ParameterCapability(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    accepted_source_units: tuple[NonEmptyString, ...]
    unit_policy_id: NonEmptyString
    quality_profile_id: NonEmptyString
    channel_policies: tuple[ChannelPolicy, ...]
    default_aggregation_by_resolution: tuple[ChannelAggregationDefault, ...]
    official_name: NonEmptyString
    official_unit: NonEmptyString
    official_group: NonEmptyString
    medium: NonEmptyString | None
    sample_fraction: NonEmptyString | None
    aliases_zh: tuple[NonEmptyString, ...]
    aliases_en: tuple[NonEmptyString, ...]
    roles: tuple[ParameterRole, ...]
    availability_providers: tuple[ParameterProvider, ...]
    observation_providers: tuple[ParameterProvider, ...]
    fixture_identity: NonEmptyString
    resolution_enabled: StrictBool

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if self.key.code_system != "usgs" or (
            len(self.key.code) != 5 or not self.key.code.isdecimal()
        ):
            raise ValueError("parameter capability key must be usgs:<five digits>")
        for field_name in (
            "accepted_source_units",
            "channel_policies",
            "default_aggregation_by_resolution",
            "aliases_zh",
            "aliases_en",
            "roles",
            "availability_providers",
            "observation_providers",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        channel_keys = tuple(
            (policy.provider, policy.channel) for policy in self.channel_policies
        )
        ensure_unique_tuple(channel_keys, field_name="channel_policies")
        default_keys = tuple(
            (default.provider, default.channel, default.resolution)
            for default in self.default_aggregation_by_resolution
        )
        ensure_unique_tuple(
            default_keys,
            field_name="default_aggregation_by_resolution",
        )
        if ParameterRole.TARGET not in self.roles:
            raise ValueError("every capability must support the target role")
        canonical = normalize_parameter_concept(self.canonical_name)
        aliases = tuple(
            normalize_parameter_concept(alias)
            for alias in (*self.aliases_zh, *self.aliases_en)
        )
        if canonical in aliases:
            raise ValueError("aliases must not duplicate canonical_name")
        if len(aliases) != len(set(aliases)):
            raise ValueError("aliases must be unique within a capability")
        if not any(
            "\u4e00" <= character <= "\u9fff"
            for alias in self.aliases_zh
            for character in alias
        ):
            raise ValueError("aliases_zh must contain a Chinese alias")
        if not any(
            character.isascii() and character.isalpha()
            for alias in self.aliases_en
            for character in alias
        ):
            raise ValueError("aliases_en must contain an English alias")
        return self
