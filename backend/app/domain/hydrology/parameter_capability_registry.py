from collections import Counter
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from app.domain.hydrology.evidence_rule_id import EvidenceRuleId
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
from app.domain.hydrology.parameter_role import ParameterRole

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


class ParameterCapabilityRegistry:
    __slots__ = (
        "_alias_index",
        "_by_key",
        "_canonical_index",
        "_capabilities",
        "_group_counts",
        "_required_support",
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
        declared_rules = manifest.evidence_rule_ids
        if len(declared_rules) != len(set(declared_rules)) or set(
            declared_rules
        ) != set(EvidenceRuleId):
            raise ParameterCapabilityConfigurationError(
                "evidence_rule_ids must declare every supported rule exactly once"
            )
        declared_rule_set = set(declared_rules)
        if any(
            not set(capability.allowed_indirect_evidence_rules)
            <= declared_rule_set
            for capability in capabilities
        ):
            raise ParameterCapabilityConfigurationError(
                "capability references an undeclared evidence rule"
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
        if len(required_support) != 1 or required_support[0].key != ParameterKey(
            code_system="usgs",
            code="00060",
        ):
            raise ParameterCapabilityConfigurationError(
                "usgs:00060 must be the unique required support capability"
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

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("parameter capability registry is immutable")
        object.__setattr__(self, name, value)

    @property
    def capabilities(self) -> tuple[ParameterCapability, ...]:
        return self._capabilities

    @property
    def group_counts(self) -> Mapping[str, int]:
        return self._group_counts

    @property
    def required_support(self) -> tuple[ParameterCapability, ...]:
        return self._required_support

    def get(self, key: ParameterKey) -> ParameterCapability | None:
        return self._by_key.get(key)

    def exact_matches(self, concept: str) -> tuple[ParameterCapability, ...]:
        normalized = normalize_parameter_concept(concept)
        return self._canonical_index.get(
            normalized,
            self._alias_index.get(normalized, ()),
        )
