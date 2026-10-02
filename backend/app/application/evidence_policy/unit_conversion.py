from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from app.application.evidence_policy.errors import EvidencePolicyError
from app.application.evidence_policy.models import UnitConversionResult
from app.domain.common import CanonicalDecimal, normalize_canonical_decimal
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_registry import (
    ParameterCapabilityRegistry,
)
from app.domain.hydrology.unit_kind import UnitKind


def convert_value(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    value: CanonicalDecimal,
    source_unit: str,
    *,
    target_unit_id: str | None = None,
) -> UnitConversionResult:
    source = registry.resolve_unit(source_unit)
    target_id = target_unit_id or capability.unit_policy_id
    target = registry.unit_definitions.get(target_id)
    if (
        source is None
        or target is None
        or source.unit_id not in capability.accepted_source_units
        or source.dimension_id != target.dimension_id
    ):
        raise EvidencePolicyError(
            "evidence_unit_unresolved",
            "source and target units are not registered as compatible",
        )
    if (
        source.kind is UnitKind.IDENTITY_ONLY
        or target.kind is UnitKind.IDENTITY_ONLY
    ) and source.unit_id != target.unit_id:
        raise EvidencePolicyError(
            "evidence_unit_unresolved",
            "identity-only units cannot be converted",
        )

    with localcontext() as context:
        context.prec = 34
        source_value = Decimal(value)
        base_value = (
            source_value * Decimal(source.scale_to_base)
            + Decimal(source.offset_to_base)
        )
        unrounded = (
            base_value - Decimal(target.offset_to_base)
        ) / Decimal(target.scale_to_base)
        quantum = Decimal(1).scaleb(-target.precision)
        rounded = unrounded.quantize(quantum, rounding=ROUND_HALF_EVEN)

    return UnitConversionResult(
        source_value=value,
        source_unit_id=source.unit_id,
        source_unit=source.symbol,
        target_unit_id=target.unit_id,
        target_unit=target.symbol,
        formula_id=f"unit:{source.unit_id}->{target.unit_id}:v1",
        registry_digest=registry.manifest_digest,
        unrounded_value=normalize_canonical_decimal(unrounded),
        value=normalize_canonical_decimal(rounded),
    )
