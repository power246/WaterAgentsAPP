import json
from collections import Counter
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from app.domain.hydrology.aggregation_profile import AggregationProfile
from app.domain.hydrology.channel_policy import ChannelPolicy
from app.domain.hydrology.coverage_profile import CoverageProfile
from app.domain.hydrology.evidence_aggregation_method import (
    EvidenceAggregationMethod,
)
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_configuration_error import (
    ParameterCapabilityConfigurationError,
)
from app.domain.hydrology.parameter_capability_manifest import (
    ParameterCapabilityManifest,
)
from app.domain.hydrology.parameter_concept_normalization import (
    normalize_parameter_concept,
)
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.parameter_role import ParameterRole
from app.domain.hydrology.quality_profile import QualityProfile
from app.domain.hydrology.unit_definition import UnitDefinition
from app.domain.hydrology.unit_kind import UnitKind
from app.domain.hydrology.unit_normalization import normalize_unit_identity
from app.domain.hydrology.value_semantics import ValueSemantics

EXPECTED_PARAMETER_CAPABILITY_COUNT = 154
EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256 = (
    "3f9a0146b474c6c637adf837774b0680c389025cb60771fae55f11ab13d2c8bb"
)
EXPECTED_PARAMETER_GROUP_COUNTS: Mapping[str, int] = MappingProxyType(
    {
        "PHY": 28,
        "INN": 10,
        "NUT": 25,
        "INM": 6,
        "IMN": 7,
        "IMM": 22,
        "MBI": 12,
        "BIO": 9,
        "OPE": 18,
        "OOT": 10,
        "RAD": 5,
        "SED": 2,
    }
)

_SUPPORTED_METHODS: Mapping[
    ValueSemantics,
    frozenset[EvidenceAggregationMethod],
] = (
    MappingProxyType(
        {
            ValueSemantics.INSTANTANEOUS: frozenset(
                {
                    EvidenceAggregationMethod.MEAN,
                    EvidenceAggregationMethod.MEDIAN,
                    EvidenceAggregationMethod.MIN,
                    EvidenceAggregationMethod.MAX,
                    EvidenceAggregationMethod.P90,
                    EvidenceAggregationMethod.LATEST,
                }
            ),
            ValueSemantics.INTERVAL_MEAN: frozenset(
                {EvidenceAggregationMethod.LATEST}
            ),
            ValueSemantics.INTERVAL_MIN: frozenset(
                {EvidenceAggregationMethod.LATEST}
            ),
            ValueSemantics.INTERVAL_MAX: frozenset(
                {EvidenceAggregationMethod.LATEST}
            ),
            ValueSemantics.INTERVAL_TOTAL: frozenset(
                {
                    EvidenceAggregationMethod.SUM,
                    EvidenceAggregationMethod.MIN,
                    EvidenceAggregationMethod.MAX,
                    EvidenceAggregationMethod.LATEST,
                }
            ),
            ValueSemantics.CUMULATIVE_COUNTER: frozenset(
                {
                    EvidenceAggregationMethod.COUNTER_DELTA_SUM,
                    EvidenceAggregationMethod.LATEST,
                }
            ),
            ValueSemantics.CATEGORICAL_STATE: frozenset(
                {EvidenceAggregationMethod.LATEST}
            ),
            ValueSemantics.FORECAST_INTERVAL: frozenset(
                {
                    EvidenceAggregationMethod.MEAN,
                    EvidenceAggregationMethod.MIN,
                    EvidenceAggregationMethod.MAX,
                    EvidenceAggregationMethod.SUM,
                    EvidenceAggregationMethod.LATEST,
                }
            ),
        }
    )
)


def _canonical_manifest_bytes(manifest: ParameterCapabilityManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json", exclude={"manifest_digest_sha256"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_profile_graph(
    *,
    profile_ids: frozenset[str],
    parent_by_id: Mapping[str, str | None],
    label: str,
) -> None:
    unknown = {
        parent
        for parent in parent_by_id.values()
        if parent is not None and parent not in profile_ids
    }
    if unknown:
        raise ParameterCapabilityConfigurationError(
            f"{label} references an unknown parent profile"
        )
    for profile_id in profile_ids:
        seen: set[str] = set()
        current: str | None = profile_id
        while current is not None:
            if current in seen:
                raise ParameterCapabilityConfigurationError(
                    f"{label} profile inheritance must not contain a cycle"
                )
            seen.add(current)
            current = parent_by_id[current]


class ParameterCapabilityRegistry:
    __slots__ = (
        "_aggregation_profiles",
        "_alias_index",
        "_by_key",
        "_canonical_index",
        "_canonical_manifest_bytes",
        "_capabilities",
        "_coverage_profiles",
        "_group_counts",
        "_manifest_digest",
        "_quality_profiles",
        "_required_support",
        "_unit_alias_index",
        "_unit_definitions",
    )

    def __init__(self, manifest: ParameterCapabilityManifest) -> None:
        capabilities = manifest.capabilities
        if len(capabilities) != EXPECTED_PARAMETER_CAPABILITY_COUNT:
            raise ParameterCapabilityConfigurationError(
                "parameter capability manifest must contain exactly 154 entries"
            )
        keys = tuple(capability.key for capability in capabilities)
        if len(keys) != len(set(keys)):
            raise ParameterCapabilityConfigurationError(
                "parameter capability keys must be unique"
            )
        fixture_identities = tuple(
            capability.fixture_identity for capability in capabilities
        )
        if len(fixture_identities) != len(set(fixture_identities)):
            raise ParameterCapabilityConfigurationError(
                "parameter capability fixture identities must be unique"
            )
        digest = sha256(
            "\n".join(
                sorted(f"{key.code_system}:{key.code}" for key in keys)
            ).encode("utf-8")
        ).hexdigest()
        if (
            manifest.catalog_key_set_sha256 != digest
            or digest != EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256
        ):
            raise ParameterCapabilityConfigurationError(
                "parameter capability key set does not match the M4 catalog"
            )
        group_counts = Counter(
            capability.official_group for capability in capabilities
        )
        if dict(group_counts) != dict(EXPECTED_PARAMETER_GROUP_COUNTS):
            raise ParameterCapabilityConfigurationError(
                "parameter capability group counts do not match the M4 catalog"
            )

        manifest_bytes = _canonical_manifest_bytes(manifest)
        manifest_digest = sha256(manifest_bytes).hexdigest()
        if manifest.manifest_digest_sha256 != manifest_digest:
            raise ParameterCapabilityConfigurationError(
                "parameter capability manifest digest does not match canonical content"
            )

        unit_by_id = self._index_models(
            manifest.unit_definitions,
            id_attribute="unit_id",
            label="unit definition",
        )
        quality_by_id = self._index_models(
            manifest.quality_profiles,
            id_attribute="profile_id",
            label="quality profile",
        )
        aggregation_by_id = self._index_models(
            manifest.aggregation_profiles,
            id_attribute="profile_id",
            label="aggregation profile",
        )
        coverage_by_id = self._index_models(
            manifest.coverage_profiles,
            id_attribute="profile_id",
            label="coverage profile",
        )
        self._validate_profiles(
            manifest.quality_profiles,
            profile_ids=frozenset(quality_by_id),
            label="quality",
        )
        self._validate_profiles(
            manifest.aggregation_profiles,
            profile_ids=frozenset(aggregation_by_id),
            label="aggregation",
        )
        self._validate_profiles(
            manifest.coverage_profiles,
            profile_ids=frozenset(coverage_by_id),
            label="coverage",
        )
        unit_alias_index = self._validate_unit_aliases(manifest.unit_definitions)

        for capability in capabilities:
            self._validate_capability_policy(
                capability,
                unit_by_id=unit_by_id,
                quality_by_id=quality_by_id,
                aggregation_by_id=aggregation_by_id,
                coverage_by_id=coverage_by_id,
            )

        if not all(
            capability.resolution_enabled for capability in capabilities
        ):
            raise ParameterCapabilityConfigurationError(
                "all M4 baseline capabilities must be resolution-enabled"
            )
        required_support = tuple(
            capability
            for capability in capabilities
            if ParameterRole.REQUIRED_SUPPORT in capability.roles
        )
        discharge_key = ParameterKey(code_system="usgs", code="00060")
        if len(required_support) != 1 or required_support[0].key != discharge_key:
            raise ParameterCapabilityConfigurationError(
                "usgs:00060 must be the unique required support capability"
            )
        discharge = required_support[0]
        if not any(
            policy.direct_observation for policy in discharge.channel_policies
        ):
            raise ParameterCapabilityConfigurationError(
                "usgs:00060 requires a direct channel"
            )

        by_key = {capability.key: capability for capability in capabilities}
        canonical_names: dict[str, list[ParameterCapability]] = {}
        aliases: dict[str, list[ParameterCapability]] = {}
        for capability in capabilities:
            canonical_names.setdefault(
                normalize_parameter_concept(capability.canonical_name), []
            ).append(capability)
            for name in (*capability.aliases_zh, *capability.aliases_en):
                aliases.setdefault(normalize_parameter_concept(name), []).append(
                    capability
                )
        if any(len(matches) != 1 for matches in canonical_names.values()):
            raise ParameterCapabilityConfigurationError(
                "parameter capability canonical names must be unique"
            )

        self._capabilities = capabilities
        self._by_key = MappingProxyType(by_key)
        self._canonical_index = MappingProxyType(
            {name: tuple(matches) for name, matches in canonical_names.items()}
        )
        self._alias_index = MappingProxyType(
            {name: tuple(matches) for name, matches in aliases.items()}
        )
        self._group_counts = MappingProxyType(dict(group_counts))
        self._required_support = required_support
        self._unit_definitions = MappingProxyType(unit_by_id)
        self._unit_alias_index = MappingProxyType(unit_alias_index)
        self._quality_profiles = MappingProxyType(quality_by_id)
        self._aggregation_profiles = MappingProxyType(aggregation_by_id)
        self._coverage_profiles = MappingProxyType(coverage_by_id)
        self._canonical_manifest_bytes = manifest_bytes
        self._manifest_digest = manifest_digest

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("parameter capability registry is immutable")
        object.__setattr__(self, name, value)

    @staticmethod
    def _index_models(
        values: tuple[object, ...],
        *,
        id_attribute: str,
        label: str,
    ) -> dict[str, object]:
        indexed = {str(getattr(value, id_attribute)): value for value in values}
        if not values or len(indexed) != len(values):
            raise ParameterCapabilityConfigurationError(
                f"{label} identifiers must be non-empty and unique"
            )
        return indexed

    @staticmethod
    def _validate_profiles(
        profiles: tuple[QualityProfile, ...]
        | tuple[AggregationProfile, ...]
        | tuple[CoverageProfile, ...],
        *,
        profile_ids: frozenset[str],
        label: str,
    ) -> None:
        _validate_profile_graph(
            profile_ids=profile_ids,
            parent_by_id={
                profile.profile_id: profile.extends_profile_id
                for profile in profiles
            },
            label=label,
        )

    @staticmethod
    def _validate_unit_aliases(
        units: tuple[UnitDefinition, ...],
    ) -> dict[str, UnitDefinition]:
        aliases: dict[str, UnitDefinition] = {}
        for unit in units:
            for raw_name in (unit.symbol, *unit.aliases):
                name = normalize_unit_identity(raw_name)
                if name in aliases:
                    raise ParameterCapabilityConfigurationError(
                        "unit symbols and aliases must be globally unique"
                    )
                aliases[name] = unit
        return aliases

    @staticmethod
    def _validate_capability_policy(
        capability: ParameterCapability,
        *,
        unit_by_id: Mapping[str, object],
        quality_by_id: Mapping[str, object],
        aggregation_by_id: Mapping[str, object],
        coverage_by_id: Mapping[str, object],
    ) -> None:
        if capability.unit_policy_id not in unit_by_id:
            raise ParameterCapabilityConfigurationError(
                "capability references an unknown canonical unit"
            )
        if not set(capability.accepted_source_units) <= set(unit_by_id):
            raise ParameterCapabilityConfigurationError(
                "capability references an unknown accepted source unit"
            )
        canonical_unit = unit_by_id[capability.unit_policy_id]
        if not isinstance(canonical_unit, UnitDefinition):
            raise ParameterCapabilityConfigurationError("invalid unit registry")
        accepted_units = tuple(
            unit_by_id[unit_id] for unit_id in capability.accepted_source_units
        )
        if any(not isinstance(unit, UnitDefinition) for unit in accepted_units):
            raise ParameterCapabilityConfigurationError("invalid unit registry")
        if capability.unit_policy_id not in capability.accepted_source_units:
            raise ParameterCapabilityConfigurationError(
                "accepted source units must include the canonical unit"
            )
        if any(
            unit.dimension_id != canonical_unit.dimension_id
            for unit in accepted_units
            if isinstance(unit, UnitDefinition)
        ):
            raise ParameterCapabilityConfigurationError(
                "accepted source unit dimension does not match canonical unit"
            )
        canonical_names = {
            normalize_unit_identity(value)
            for value in (canonical_unit.symbol, *canonical_unit.aliases)
        }
        if normalize_unit_identity(capability.canonical_unit) not in canonical_names:
            raise ParameterCapabilityConfigurationError(
                "canonical unit text does not match unit policy"
            )
        if canonical_unit.kind == UnitKind.IDENTITY_ONLY and len(accepted_units) != 1:
            raise ParameterCapabilityConfigurationError(
                "identity-only unit cannot accept convertible source units"
            )
        if capability.quality_profile_id not in quality_by_id:
            raise ParameterCapabilityConfigurationError(
                "capability references an unknown quality profile"
            )
        policies = {
            (policy.provider, policy.channel): policy
            for policy in capability.channel_policies
        }
        if not policies or {key[0] for key in policies} != set(
            capability.observation_providers
        ):
            raise ParameterCapabilityConfigurationError(
                "each observation provider requires an actual channel policy"
            )
        for policy in policies.values():
            if (
                policy.quality_profile_id not in quality_by_id
                or policy.aggregation_profile_id not in aggregation_by_id
                or policy.coverage_profile_id not in coverage_by_id
            ):
                raise ParameterCapabilityConfigurationError(
                    "channel references an unknown policy profile"
                )
            profile = aggregation_by_id[policy.aggregation_profile_id]
            if not isinstance(profile, AggregationProfile):
                raise ParameterCapabilityConfigurationError(
                    "invalid aggregation profile registry"
                )
            supported_methods = _SUPPORTED_METHODS[policy.value_semantics]
            if not set(profile.allowed_methods) <= supported_methods:
                raise ParameterCapabilityConfigurationError(
                    "aggregation method is incompatible with value semantics"
                )
            if (
                canonical_unit.dimension_id
                in {"mass_concentration", "ph", "volumetric_flow", "count_density"}
                and policy.value_semantics
                in {
                    ValueSemantics.INTERVAL_TOTAL,
                    ValueSemantics.CUMULATIVE_COUNTER,
                }
            ):
                raise ParameterCapabilityConfigurationError(
                    "unit dimension is incompatible with value semantics"
                )
            if policy.medium_constraint != capability.medium or (
                policy.fraction_constraint != capability.sample_fraction
            ):
                raise ParameterCapabilityConfigurationError(
                    "channel medium/fraction constraints must match capability"
                )
            if not policy.direct_observation:
                raise ParameterCapabilityConfigurationError(
                    "registered observation channels must be direct"
                )

        defaults = {
            (default.provider, default.channel, default.resolution): default
            for default in capability.default_aggregation_by_resolution
        }
        expected_defaults = {
            (provider, channel, resolution)
            for provider, channel in policies
            for resolution in ("6h", "12h", "24h")
        }
        if set(defaults) != expected_defaults:
            raise ParameterCapabilityConfigurationError(
                "every channel requires one default for each time resolution"
            )
        for key, default in defaults.items():
            policy = policies[(key[0], key[1])]
            profile = aggregation_by_id[policy.aggregation_profile_id]
            if not isinstance(profile, AggregationProfile) or (
                default.method not in profile.allowed_methods
            ):
                raise ParameterCapabilityConfigurationError(
                    "channel default is not allowed by its aggregation profile"
                )

    @property
    def capabilities(self) -> tuple[ParameterCapability, ...]:
        return self._capabilities

    @property
    def group_counts(self) -> Mapping[str, int]:
        return self._group_counts

    @property
    def required_support(self) -> tuple[ParameterCapability, ...]:
        return self._required_support

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    @property
    def canonical_manifest_bytes(self) -> bytes:
        return self._canonical_manifest_bytes

    @property
    def unit_definitions(self) -> Mapping[str, UnitDefinition]:
        return self._unit_definitions

    @property
    def quality_profiles(self) -> Mapping[str, QualityProfile]:
        return self._quality_profiles

    @property
    def aggregation_profiles(self) -> Mapping[str, AggregationProfile]:
        return self._aggregation_profiles

    @property
    def coverage_profiles(self) -> Mapping[str, CoverageProfile]:
        return self._coverage_profiles

    def get(self, key: ParameterKey) -> ParameterCapability | None:
        return self._by_key.get(key)

    def exact_matches(self, concept: str) -> tuple[ParameterCapability, ...]:
        normalized = normalize_parameter_concept(concept)
        return self._canonical_index.get(
            normalized,
            self._alias_index.get(normalized, ()),
        )

    def resolve_unit(self, symbol_or_alias: str) -> UnitDefinition | None:
        return self._unit_alias_index.get(normalize_unit_identity(symbol_or_alias))

    def get_channel_policy(
        self,
        key: ParameterKey,
        provider: ParameterProvider,
        channel: str,
    ) -> ChannelPolicy | None:
        capability = self.get(key)
        if capability is None:
            return None
        return next(
            (
                policy
                for policy in capability.channel_policies
                if policy.provider == provider and policy.channel == channel
            ),
            None,
        )
