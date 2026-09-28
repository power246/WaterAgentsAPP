from collections import Counter

import pytest
from pydantic import ValidationError

from app.domain.hydrology import (
    EXPECTED_PARAMETER_CAPABILITY_COUNT,
    EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256,
    EXPECTED_PARAMETER_GROUP_COUNTS,
    EvidenceRuleId,
    ParameterCapabilityManifest,
    ParameterCapabilityRegistry,
    ParameterProvider,
    ParameterRole,
)
from app.infrastructure.config import load_parameter_capability_registry


def keys(matches) -> tuple[str, ...]:
    return tuple(match.key.code for match in matches)


def test_manifest_matches_the_complete_m4_catalog_contract() -> None:
    registry = load_parameter_capability_registry()

    assert len(registry.capabilities) == EXPECTED_PARAMETER_CAPABILITY_COUNT == 154
    assert dict(registry.group_counts) == dict(EXPECTED_PARAMETER_GROUP_COUNTS)
    assert sum(registry.group_counts.values()) == 154
    assert keys(registry.required_support) == ("00060",)
    assert all(
        capability.resolution_enabled for capability in registry.capabilities
    )
    assert all(capability.key.code_system == "usgs" for capability in registry.capabilities)
    assert len({capability.key for capability in registry.capabilities}) == 154
    assert Counter(capability.official_group for capability in registry.capabilities) == Counter(
        EXPECTED_PARAMETER_GROUP_COUNTS
    )


def test_every_capability_has_complete_routes_aliases_evidence_and_fixture() -> None:
    registry = load_parameter_capability_registry()

    for capability in registry.capabilities:
        assert capability.canonical_name
        assert capability.canonical_unit
        assert capability.official_name
        assert capability.official_unit
        assert capability.official_group
        assert capability.aliases_zh
        assert capability.aliases_en
        assert capability.availability_providers
        assert capability.observation_providers
        assert capability.allowed_indirect_evidence_rules
        assert capability.fixture_identity == (
            f"usgs_parameter_{capability.key.code}_v1"
        )
        assert set(capability.availability_providers) <= set(ParameterProvider)
        assert set(capability.observation_providers) <= set(ParameterProvider)
        assert set(capability.allowed_indirect_evidence_rules) <= set(EvidenceRuleId)
        assert ParameterRole.TARGET in capability.roles


def test_canonical_names_win_then_explicit_alias_collisions_remain_ambiguous() -> None:
    registry = load_parameter_capability_registry()

    assert keys(registry.exact_matches("  water TEMPERATURE ")) == ("00010",)
    assert keys(registry.exact_matches("水温")) == ("00010",)
    assert keys(registry.exact_matches("pH")) == ("00400",)
    assert keys(registry.exact_matches("turbidity")) == ("61028", "63680")
    assert keys(registry.exact_matches("arsenic")) == ("01000", "01002")
    assert keys(registry.exact_matches("e coli")) == (
        "31633",
        "50468",
        "90901",
    )


@pytest.mark.parametrize(
    ("concept", "expected_code"),
    [
        ("pH, unfiltered, laboratory", "00403"),
        ("Turbidity, field", "61028"),
        ("Turbidity, Formazin Nephelometric", "63680"),
        ("Arsenic, filtered", "01000"),
        ("Arsenic, unfiltered", "01002"),
        ("Escherichia coli, m-TEC MF", "31633"),
        ("E. coli, defined substrate test", "50468"),
        ("E. coli, MI MF", "90901"),
    ],
)
def test_method_fraction_and_unit_specific_names_are_not_merged(
    concept: str,
    expected_code: str,
) -> None:
    assert keys(load_parameter_capability_registry().exact_matches(concept)) == (
        expected_code,
    )


def test_manifest_models_reject_unknown_provider_rule_and_duplicate_alias() -> None:
    registry = load_parameter_capability_registry()
    capability = registry.capabilities[0]
    payload = capability.model_dump(mode="json")

    payload["availability_providers"] = ["unknown_provider"]
    with pytest.raises(ValidationError, match="unknown_provider"):
        type(capability).model_validate(payload)

    payload = capability.model_dump(mode="json")
    payload["allowed_indirect_evidence_rules"] = ["unknown_rule"]
    with pytest.raises(ValidationError, match="unknown_rule"):
        type(capability).model_validate(payload)

    payload = capability.model_dump(mode="json")
    payload["aliases_en"] = ["duplicate", "duplicate"]
    with pytest.raises(ValidationError, match="aliases"):
        type(capability).model_validate(payload)


def test_registry_rejects_a_manifest_with_the_wrong_catalog_digest() -> None:
    registry = load_parameter_capability_registry()
    manifest = ParameterCapabilityManifest(
        schema_version=1,
        catalog_key_set_sha256="0" * 64,
        evidence_rule_ids=tuple(EvidenceRuleId),
        capabilities=registry.capabilities,
    )

    with pytest.raises(ValueError, match="key set"):
        ParameterCapabilityRegistry(manifest)

    assert EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256 != "0" * 64


def test_registry_is_immutable_after_construction() -> None:
    registry = load_parameter_capability_registry()

    with pytest.raises(AttributeError, match="immutable"):
        registry._capabilities = ()
    with pytest.raises(TypeError):
        registry.group_counts["PHY"] = 0
