from collections import Counter
from hashlib import sha256
from importlib.resources import files
import json

import pytest
from pydantic import ValidationError

from app.domain.hydrology import (
    EXPECTED_PARAMETER_CAPABILITY_COUNT,
    EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256,
    EXPECTED_PARAMETER_GROUP_COUNTS,
    EvidenceAggregationMethod,
    ParameterCapabilityManifest,
    ParameterCapabilityRegistry,
    ParameterProvider,
    ParameterRole,
    UnitKind,
    ValueSemantics,
)
from app.infrastructure.config import (
    load_parameter_capability_registry,
    load_parameter_capability_registry_from_bytes,
)


def manifest_payload() -> dict[str, object]:
    resource = files("app.infrastructure.config").joinpath(
        "parameter_capabilities.v1.json"
    )
    return json.loads(resource.read_text(encoding="utf-8"))


def encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def resign_manifest(payload: dict[str, object]) -> None:
    content = dict(payload)
    content.pop("manifest_digest_sha256")
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    payload["manifest_digest_sha256"] = sha256(canonical).hexdigest()


def keys(matches) -> tuple[str, ...]:
    return tuple(match.key.code for match in matches)


def test_manifest_matches_the_complete_m4_catalog_contract() -> None:
    registry = load_parameter_capability_registry()
    payload = manifest_payload()

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
    assert "evidence_rule_ids" not in payload
    assert "evidence_relations" not in payload
    assert all(
        "allowed_indirect_evidence_rules" not in item
        for item in payload["capabilities"]
    )


def test_every_capability_has_complete_routes_aliases_and_fixture() -> None:
    registry = load_parameter_capability_registry()

    for capability in registry.capabilities:
        assert capability.canonical_name
        assert capability.canonical_unit
        assert capability.accepted_source_units
        assert capability.unit_policy_id in registry.unit_definitions
        assert capability.quality_profile_id in registry.quality_profiles
        assert capability.channel_policies
        assert capability.default_aggregation_by_resolution
        assert capability.official_name
        assert capability.official_unit
        assert capability.official_group
        assert capability.aliases_zh
        assert capability.aliases_en
        assert capability.availability_providers
        assert capability.observation_providers
        assert capability.fixture_identity == (
            f"usgs_parameter_{capability.key.code}_v1"
        )
        assert set(capability.availability_providers) <= set(ParameterProvider)
        assert set(capability.observation_providers) <= set(ParameterProvider)
        assert ParameterRole.TARGET in capability.roles
        assert {policy.provider for policy in capability.channel_policies} == set(
            capability.observation_providers
        )
        assert len(capability.default_aggregation_by_resolution) == (
            3 * len(capability.channel_policies)
        )
        for policy in capability.channel_policies:
            assert policy.aggregation_profile_id in registry.aggregation_profiles
            assert policy.coverage_profile_id in registry.coverage_profiles
            assert policy.quality_profile_id in registry.quality_profiles
            assert policy.accepted_methods
            assert policy.direct_observation is True


def test_manifest_policy_registry_is_complete_and_reference_closed() -> None:
    registry = load_parameter_capability_registry()

    assert len(registry.unit_definitions) == 45
    assert len(registry.quality_profiles) == 2
    assert len(registry.aggregation_profiles) == 10
    assert len(registry.coverage_profiles) == 5
    assert len(registry.manifest_digest) == 64
    assert all(profile.valid_tokens for profile in registry.quality_profiles.values())
    assert all(
        isinstance(profile.allow_partial_coverage, bool)
        for profile in registry.aggregation_profiles.values()
    )
    assert registry.resolve_unit("  °C ").unit_id == "degree_celsius"
    assert registry.resolve_unit("μg/L").unit_id == "micrograms_per_liter"
    assert registry.resolve_unit("not-a-unit") is None

def test_required_discharge_has_direct_channels() -> None:
    registry = load_parameter_capability_registry()
    discharge = registry.required_support[0]

    assert discharge.key.code == "00060"
    assert all(policy.direct_observation for policy in discharge.channel_policies)


def test_representative_policy_families_reject_obvious_sum_misuse() -> None:
    registry = load_parameter_capability_registry()

    def capability(code: str):
        return next(
            item for item in registry.capabilities if item.key.code == code
        )

    ph = capability("00400")
    assert registry.unit_definitions[ph.unit_policy_id].kind == UnitKind.IDENTITY_ONLY
    assert all(
        EvidenceAggregationMethod.SUM
        not in registry.aggregation_profiles[policy.aggregation_profile_id].allowed_methods
        for policy in ph.channel_policies
    )

    discharge = capability("00060")
    assert all(
        EvidenceAggregationMethod.SUM
        not in registry.aggregation_profiles[policy.aggregation_profile_id].allowed_methods
        for policy in discharge.channel_policies
    )

    precipitation = capability("00045")
    semantics = {
        policy.channel: policy.value_semantics
        for policy in precipitation.channel_policies
    }
    assert semantics == {
        "continuous": ValueSemantics.CUMULATIVE_COUNTER,
        "daily": ValueSemantics.INTERVAL_TOTAL,
    }
    assert registry.get_channel_policy(
        precipitation.key,
        precipitation.observation_providers[0],
        "continuous",
    ).aggregation_profile_id == "cumulative_counter"
    assert registry.get_channel_policy(
        precipitation.key,
        precipitation.observation_providers[0],
        "daily",
    ).aggregation_profile_id == "interval_total"

    daily_temperature = registry.get_channel_policy(
        capability("00010").key,
        capability("00010").observation_providers[0],
        "daily",
    )
    assert daily_temperature.value_semantics == ValueSemantics.INTERVAL_MEAN
    assert daily_temperature.aggregation_profile_id == "provider_interval_statistic"

    for code in ("00300", "31633"):
        sample = next(
            policy
            for policy in capability(code).channel_policies
            if policy.channel == "discrete_samples"
        )
        assert EvidenceAggregationMethod.SUM not in registry.aggregation_profiles[
            sample.aggregation_profile_id
        ].allowed_methods


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


def test_manifest_models_reject_unknown_provider_and_duplicate_alias() -> None:
    registry = load_parameter_capability_registry()
    capability = registry.capabilities[0]
    payload = capability.model_dump(mode="json")

    payload["availability_providers"] = ["unknown_provider"]
    with pytest.raises(ValidationError, match="unknown_provider"):
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
        manifest_digest_sha256=registry.manifest_digest,
        unit_definitions=tuple(registry.unit_definitions.values()),
        quality_profiles=tuple(registry.quality_profiles.values()),
        aggregation_profiles=tuple(registry.aggregation_profiles.values()),
        coverage_profiles=tuple(registry.coverage_profiles.values()),
        capabilities=registry.capabilities,
    )

    with pytest.raises(ValueError, match="key set"):
        ParameterCapabilityRegistry(manifest)

    assert EXPECTED_PARAMETER_CATALOG_KEY_SET_SHA256 != "0" * 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unknown_profile", "unknown policy profile"),
        ("profile_cycle", "cycle"),
        ("dimension_mismatch", "dimension"),
        ("missing_channel", "invalid parameter capability manifest"),
        ("illegal_method", "incompatible with value semantics"),
        ("illegal_semantics", "incompatible with value semantics"),
        ("duplicate_unit_alias", "globally unique"),
        ("digest_mismatch", "digest does not match"),
    ],
)
def test_registry_rejects_invalid_policy_configuration(
    mutation: str,
    message: str,
) -> None:
    payload = manifest_payload()
    capabilities = payload["capabilities"]

    if mutation == "unknown_profile":
        capabilities[0]["channel_policies"][0]["aggregation_profile_id"] = "missing"
    elif mutation == "profile_cycle":
        payload["aggregation_profiles"][0]["extends_profile_id"] = payload[
            "aggregation_profiles"
        ][0]["profile_id"]
    elif mutation == "dimension_mismatch":
        capabilities[0]["accepted_source_units"].append("standard_ph_unit")
    elif mutation == "missing_channel":
        capabilities[0]["channel_policies"] = []
    elif mutation == "illegal_method":
        profile = next(
            item
            for item in payload["aggregation_profiles"]
            if item["profile_id"] == "continuous_level"
        )
        profile["allowed_methods"].append("sum")
    elif mutation == "illegal_semantics":
        capabilities[0]["channel_policies"][0]["value_semantics"] = "interval_total"
    elif mutation == "duplicate_unit_alias":
        payload["unit_definitions"][1]["aliases"].append(
            payload["unit_definitions"][0]["symbol"]
        )
    elif mutation == "digest_mismatch":
        payload["manifest_digest_sha256"] = "0" * 64

    if mutation != "digest_mismatch":
        resign_manifest(payload)

    with pytest.raises(ValueError, match=message):
        load_parameter_capability_registry_from_bytes(encoded(payload))


def test_manifest_digest_is_stable_across_json_formatting_and_key_order() -> None:
    payload = manifest_payload()
    compact = encoded(payload)
    pretty_reversed = json.dumps(
        dict(reversed(tuple(payload.items()))),
        ensure_ascii=False,
        indent=4,
    ).encode("utf-8")

    first = load_parameter_capability_registry_from_bytes(compact)
    second = load_parameter_capability_registry_from_bytes(pretty_reversed)

    assert first.manifest_digest == second.manifest_digest
    assert first.canonical_manifest_bytes == second.canonical_manifest_bytes


def test_registry_is_immutable_after_construction() -> None:
    registry = load_parameter_capability_registry()

    with pytest.raises(AttributeError, match="immutable"):
        registry._capabilities = ()
    with pytest.raises(TypeError):
        registry.group_counts["PHY"] = 0
