from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext

from app.application.evidence_policy.coverage import calculate_coverage
from app.application.evidence_policy.errors import EvidencePolicyError
from app.application.evidence_policy.models import (
    AggregationOutcome,
    PolicyObservation,
)
from app.application.evidence_policy.unit_conversion import convert_value
from app.domain.common import normalize_canonical_decimal
from app.domain.evidence.aggregate_payload import AggregatePayload
from app.domain.evidence.coverage_status import CoverageStatus
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.evidence_derivation import EvidenceDerivation
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.hydrology.evidence_aggregation_method import (
    EvidenceAggregationMethod,
)
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_registry import (
    ParameterCapabilityRegistry,
)
from app.domain.time.time_range import TimeRange


def _blocked(message: str) -> EvidencePolicyError:
    return EvidencePolicyError("evidence_aggregation_blocked", message)


def _observation_time(observation: PolicyObservation) -> datetime:
    if observation.observed_time is not None:
        return observation.observed_time
    if observation.valid_time_range is None:
        raise _blocked("observation time is missing")
    return observation.valid_time_range.end


def _validate_series(
    observations: tuple[PolicyObservation, ...],
    capability: ParameterCapability,
    window: TimeRange,
) -> None:
    if not observations:
        raise _blocked("aggregation requires observations")
    reference = observations[0]
    identity = (
        reference.parameter,
        reference.provider,
        reference.channel,
        reference.station_id,
        reference.medium,
        reference.fraction,
        reference.method,
        reference.statistic,
        reference.value_semantics,
        reference.series_identity,
    )
    if reference.parameter != capability.key:
        raise _blocked("observation parameter does not match capability")
    for observation in observations:
        current = (
            observation.parameter,
            observation.provider,
            observation.channel,
            observation.station_id,
            observation.medium,
            observation.fraction,
            observation.method,
            observation.statistic,
            observation.value_semantics,
            observation.series_identity,
        )
        if current != identity:
            raise _blocked("aggregation inputs must share one series identity")
        if observation.observed_time is not None:
            if not window.contains(observation.observed_time):
                raise _blocked("point observation is outside the target window")
        elif (
            observation.valid_time_range is None
            or not observation.valid_time_range.overlaps(window)
        ):
            raise _blocked("interval observation does not overlap the target window")
    if len({observation.record_ref for observation in observations}) != len(
        observations
    ):
        raise _blocked("aggregation input record references must be unique")


def _reject_overlapping_intervals(
    observations: tuple[PolicyObservation, ...],
    window: TimeRange,
) -> None:
    ranges = sorted(
        (
            observation.valid_time_range
            for observation in observations
            if observation.valid_time_range is not None
        ),
        key=lambda item: (item.start, item.end),
    )
    if len(ranges) != len(observations):
        raise _blocked("interval-total aggregation requires interval records")
    if any(item.start < window.start or item.end > window.end for item in ranges):
        raise _blocked("interval totals must be fully contained in the target window")
    if any(left.end > right.start for left, right in zip(ranges, ranges[1:])):
        raise _blocked("interval-total aggregation does not allow overlaps")


def _aggregate_values(
    method: EvidenceAggregationMethod,
    ordered: tuple[tuple[PolicyObservation, Decimal], ...],
) -> tuple[Decimal, bool]:
    values = tuple(value for _, value in ordered)
    if method is EvidenceAggregationMethod.MEAN:
        return sum(values, Decimal("0")) / Decimal(len(values)), False
    if method is EvidenceAggregationMethod.MEDIAN:
        sorted_values = sorted(values)
        midpoint = len(sorted_values) // 2
        if len(sorted_values) % 2:
            return sorted_values[midpoint], False
        return (
            sorted_values[midpoint - 1] + sorted_values[midpoint]
        ) / Decimal("2"), False
    if method is EvidenceAggregationMethod.MIN:
        return min(values), False
    if method is EvidenceAggregationMethod.MAX:
        return max(values), False
    if method is EvidenceAggregationMethod.SUM:
        return sum(values, Decimal("0")), False
    if method is EvidenceAggregationMethod.P90:
        rank = int(
            (Decimal("0.9") * Decimal(len(values))).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        return sorted(values)[rank - 1], False
    if method is EvidenceAggregationMethod.LATEST:
        latest_time = max(_observation_time(observation) for observation, _ in ordered)
        candidates = tuple(
            value
            for observation, value in ordered
            if _observation_time(observation) == latest_time
        )
        if len(candidates) != 1:
            raise _blocked("latest observation is not unique")
        return candidates[0], False
    if method is EvidenceAggregationMethod.COUNTER_DELTA_SUM:
        if len(ordered) < 2:
            raise _blocked("counter aggregation requires at least two observations")
        if any(observation.observed_time is None for observation, _ in ordered):
            raise _blocked("counter aggregation requires point observations")
        reset = False
        total = Decimal("0")
        for (previous, previous_value), (current, current_value) in zip(
            ordered,
            ordered[1:],
        ):
            if previous.observed_time == current.observed_time:
                raise _blocked("counter values at the same time are ambiguous")
            delta = current_value - previous_value
            if delta < 0:
                reset = True
                total += current_value
            else:
                total += delta
        return total, reset
    raise _blocked("unsupported aggregation method")


def aggregate_observations(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    observations: tuple[PolicyObservation, ...],
    window: TimeRange,
    method: EvidenceAggregationMethod,
    *,
    collection_complete: bool,
    expected_cadence_seconds: int | None = None,
) -> AggregationOutcome:
    _validate_series(observations, capability, window)
    reference = observations[0]
    policy = registry.get_channel_policy(
        capability.key,
        reference.provider,
        reference.channel,
    )
    if policy is None:
        raise _blocked("provider channel is not registered")
    profile = registry.aggregation_profiles[policy.aggregation_profile_id]
    if method not in profile.allowed_methods:
        raise _blocked("aggregation method is not allowed by the profile")
    if reference.value_semantics is not policy.value_semantics:
        raise _blocked("value semantics do not match the channel policy")
    if reference.statistic not in policy.accepted_statistics:
        raise _blocked("provider statistic is not accepted")
    if reference.method not in policy.accepted_methods:
        raise _blocked("provider method is not accepted")
    if (
        reference.medium != policy.medium_constraint
        or reference.fraction != policy.fraction_constraint
    ):
        raise _blocked("medium or fraction does not match the channel policy")
    if not collection_complete:
        raise EvidencePolicyError(
            "evidence_collection_incomplete",
            "aggregation requires completed collection evidence",
        )

    converted = {}
    effective_observations = []
    unit_unresolved = False
    for observation in observations:
        if observation.quality.quality_class is EvidenceQualityClass.INVALID:
            effective_observations.append(observation)
            continue
        try:
            converted[observation.record_ref] = convert_value(
                registry,
                capability,
                observation.value,
                observation.unit,
            )
        except EvidencePolicyError as error:
            if error.code != "evidence_unit_unresolved":
                raise
            unit_unresolved = True
            effective_observations.append(observation.model_copy(update={
                "quality": observation.quality.model_copy(update={
                    "quality_class": EvidenceQualityClass.INVALID,
                    "flags": tuple(sorted({
                        *observation.quality.flags,
                        DataQualityFlag.UNIT_UNRESOLVED,
                    }, key=lambda flag: flag.value)),
                }),
            }))
        else:
            effective_observations.append(observation)
    effective = tuple(effective_observations)

    coverage = calculate_coverage(
        registry,
        capability,
        reference.provider,
        reference.channel,
        effective,
        window,
        collection_complete=collection_complete,
        expected_cadence_seconds=expected_cadence_seconds,
    )
    if coverage.status in {CoverageStatus.INSUFFICIENT, CoverageStatus.UNKNOWN}:
        raise EvidencePolicyError(
            "evidence_coverage_insufficient",
            "aggregation coverage is insufficient or unknown",
        )
    if coverage.status is CoverageStatus.PARTIAL and not profile.allow_partial_coverage:
        raise EvidencePolicyError(
            "evidence_coverage_insufficient",
            "the aggregation profile requires complete coverage",
        )

    valid_count = sum(
        observation.quality.quality_class is EvidenceQualityClass.VALID
        for observation in effective
    )
    suspicious_count = sum(
        observation.quality.quality_class is EvidenceQualityClass.SUSPICIOUS
        for observation in effective
    )
    invalid_count = sum(
        observation.quality.quality_class is EvidenceQualityClass.INVALID
        for observation in effective
    )
    usable = tuple(
        observation
        for observation in effective
        if observation.quality.quality_class is not EvidenceQualityClass.INVALID
    )
    if not usable:
        raise _blocked("aggregation has no usable inputs")
    if method is EvidenceAggregationMethod.SUM and (
        reference.value_semantics.value == "interval_total"
    ):
        _reject_overlapping_intervals(usable, window)

    ordered = tuple(
        sorted(
            [
                (
                    observation,
                    Decimal(converted[observation.record_ref].unrounded_value),
                )
                for observation in usable
            ],
            key=lambda item: (
                _observation_time(item[0]),
                item[0].record_ref.item_id,
                item[0].record_ref.record_id,
            ),
        )
    )
    with localcontext() as context:
        context.prec = 34
        aggregate_value, counter_reset = _aggregate_values(method, ordered)
        target_unit = registry.unit_definitions[capability.unit_policy_id]
        quantum = Decimal(1).scaleb(-target_unit.precision)
        rounded = aggregate_value.quantize(quantum, rounding=ROUND_HALF_EVEN)

    flags = {
        flag
        for observation in usable
        for flag in observation.quality.flags
    }
    if unit_unresolved:
        flags.add(DataQualityFlag.UNIT_UNRESOLVED)
    if coverage.status is CoverageStatus.PARTIAL:
        flags.add(DataQualityFlag.PARTIAL_COVERAGE)
    if counter_reset:
        flags.add(DataQualityFlag.COUNTER_RESET)
    quality_class = (
        EvidenceQualityClass.SUSPICIOUS
        if suspicious_count or flags
        else EvidenceQualityClass.VALID
    )
    record_refs = tuple(
        observation.record_ref
        for observation in sorted(
            observations,
            key=lambda item: (
                _observation_time(item),
                item.record_ref.item_id,
                item.record_ref.record_id,
            ),
        )
    )
    return AggregationOutcome(
        payload=AggregatePayload(
            value=normalize_canonical_decimal(rounded),
            unit=target_unit.symbol,
            method=method,
            input_record_refs=record_refs,
            valid_input_count=valid_count,
            suspicious_input_count=suspicious_count,
            invalid_input_count=invalid_count,
        ),
        quality_class=quality_class,
        quality_flags=tuple(sorted(flags, key=lambda flag: flag.value)),
        coverage=coverage,
        derivation=EvidenceDerivation(
            algorithm=f"decimal_{method.value}",
            algorithm_version=1,
            policy_id=profile.profile_id,
            registry_digest=registry.manifest_digest,
        ),
    )
