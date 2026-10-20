from decimal import Decimal

import pytest

from app.application.evidence_policy import (
    EvidencePolicyError,
    assess_provider_quality,
    convert_value,
)
from app.domain.evidence import (
    DataQualityFlag,
    EvidenceQualityClass,
    ProviderQuality,
)
from app.domain.hydrology import ParameterKey, ParameterProvider
from app.infrastructure.config import load_parameter_capability_registry


REGISTRY = load_parameter_capability_registry()


def capability(code: str):
    result = REGISTRY.get(ParameterKey(code_system="usgs", code=code))
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("code", "value", "source", "target", "expected"),
    [
        ("00010", "32", "  °Ｆ  ", "degree_celsius", "0"),
        ("00060", "1", "m3/s", "cubic_foot_per_second", "35.314667"),
        ("00060", "1", "ft^3/s", "cubic_foot_per_second", "1"),
        ("00300", "1000", "μg/L", "milligram_per_liter", "1"),
        ("00065", "1", "metre", "foot", "3.28084"),
        ("00045", "25.4", "mm", "inch", "1"),
    ],
)
def test_registered_unit_conversions_are_decimal_and_alias_exact(
    code: str,
    value: str,
    source: str,
    target: str,
    expected: str,
) -> None:
    result = convert_value(
        REGISTRY,
        capability(code),
        value,
        source,
        target_unit_id=target,
    )

    assert result.value == expected
    assert result.registry_digest == REGISTRY.manifest_digest
    assert result.formula_id.startswith("unit:")
    assert Decimal(result.unrounded_value).is_finite()


def test_affine_temperature_round_trip_is_stable_at_output_precision() -> None:
    temperature = capability("00010")
    celsius = convert_value(
        REGISTRY,
        temperature,
        "77",
        "deg F",
        target_unit_id="degree_celsius",
    )
    fahrenheit = convert_value(
        REGISTRY,
        temperature,
        celsius.value,
        "deg C",
        target_unit_id="degree_fahrenheit",
    )

    assert celsius.value == "25"
    assert fahrenheit.value == "77"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2345665", "1.234566"), ("1.2345675", "1.234568")],
)
def test_output_rounding_uses_decimal_half_even(value: str, expected: str) -> None:
    result = convert_value(
        REGISTRY,
        capability("00065"),
        value,
        "ft",
        target_unit_id="foot",
    )

    assert result.value == expected


@pytest.mark.parametrize(
    ("code", "source", "target"),
    [
        ("00400", "standard units", "number"),
        ("00300", "mg/l as N", "milligram_per_liter"),
        ("00010", "uS/cm @25C", "degree_celsius"),
        ("00060", "not-a-unit", None),
    ],
)
def test_unknown_or_incompatible_units_are_never_guessed(
    code: str,
    source: str,
    target: str | None,
) -> None:
    with pytest.raises(EvidencePolicyError) as error:
        convert_value(
            REGISTRY,
            capability(code),
            "1",
            source,
            target_unit_id=target,
        )

    assert error.value.code == "evidence_unit_unresolved"


def assess(
    quality: ProviderQuality,
    *,
    provider: ParameterProvider = ParameterProvider.WDFN_TIME_SERIES,
    channel: str = "continuous",
    protocol_valid: bool = True,
):
    return assess_provider_quality(
        REGISTRY,
        capability("00300"),
        provider,
        channel,
        quality,
        protocol_valid=protocol_valid,
    )


def test_quality_mapping_normalizes_tokens_and_preserves_original_values() -> None:
    result = assess(
        ProviderQuality(
            approval="ＡＣＣＥＰＴＥＤ",
            qualifiers=(" Estimated ", "ICE"),
        )
    )

    assert result.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert result.approval == "ＡＣＣＥＰＴＥＤ"
    assert result.qualifiers == ("Estimated", "ICE")
    assert set(result.flags) == {
        DataQualityFlag.ESTIMATED,
        DataQualityFlag.POSSIBLE_FREEZING,
        DataQualityFlag.PROVIDER_FLAGGED,
    }


def test_quality_mapping_distinguishes_valid_invalid_unknown_and_protocol() -> None:
    valid = assess(ProviderQuality(approval="final"))
    invalid = assess(ProviderQuality(approval="rejected"))
    unknown = assess(ProviderQuality(approval="new-provider-token"))
    missing = assess(ProviderQuality())
    protocol = assess(ProviderQuality(approval="final"), protocol_valid=False)

    assert valid.quality_class is EvidenceQualityClass.VALID
    assert invalid.quality_class is EvidenceQualityClass.INVALID
    assert unknown.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert missing.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert protocol.quality_class is EvidenceQualityClass.INVALID
    assert DataQualityFlag.INVALID_PROTOCOL in protocol.flags


def test_samples_censoring_is_suspicious_and_auditable() -> None:
    result = assess(
        ProviderQuality(approval="approved", qualifiers=("censored",)),
        provider=ParameterProvider.USGS_SAMPLES,
        channel="discrete_samples",
    )

    assert result.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert DataQualityFlag.CENSORED in result.flags
    assert DataQualityFlag.PROVIDER_FLAGGED in result.flags
