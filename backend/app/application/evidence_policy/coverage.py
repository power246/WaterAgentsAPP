from datetime import datetime
from decimal import Decimal, ROUND_CEILING

from app.application.evidence_policy.errors import EvidencePolicyError
from app.application.evidence_policy.models import PolicyObservation
from app.domain.common import normalize_canonical_decimal
from app.domain.evidence.coverage_status import CoverageStatus
from app.domain.evidence.evidence_coverage import EvidenceCoverage
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.hydrology.coverage_policy_kind import CoveragePolicyKind
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_registry import (
    ParameterCapabilityRegistry,
)
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.time.time_range import TimeRange


def _seconds(start: datetime, end: datetime) -> int:
    delta = end - start
    if delta.microseconds:
        raise EvidencePolicyError(
            "evidence_window_invalid",
            "coverage windows must use whole-second boundaries",
        )
    return delta.days * 86400 + delta.seconds


def _union_duration(ranges: tuple[TimeRange, ...], window: TimeRange) -> int:
    clipped = sorted(
        (
            max(item.start, window.start),
            min(item.end, window.end),
        )
        for item in ranges
        if item.overlaps(window)
    )
    if not clipped:
        return 0
    total = 0
    current_start, current_end = clipped[0]
    for start, end in clipped[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += _seconds(current_start, current_end)
            current_start, current_end = start, end
    return total + _seconds(current_start, current_end)


def _largest_point_gap(
    instants: tuple[datetime, ...],
    window: TimeRange,
) -> int | None:
    if not instants:
        return None
    boundaries = (window.start, *sorted(set(instants)), window.end)
    return max(_seconds(left, right) for left, right in zip(boundaries, boundaries[1:]))


def calculate_coverage(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    provider: ParameterProvider,
    channel: str,
    observations: tuple[PolicyObservation, ...],
    window: TimeRange,
    *,
    collection_complete: bool,
    expected_cadence_seconds: int | None = None,
) -> EvidenceCoverage:
    policy = registry.get_channel_policy(capability.key, provider, channel)
    if policy is None:
        raise EvidencePolicyError(
            "evidence_channel_unregistered",
            "provider channel is not registered for the capability",
        )
    profile = registry.coverage_profiles[policy.coverage_profile_id]
    window_seconds = _seconds(window.start, window.end)
    if expected_cadence_seconds is not None and expected_cadence_seconds <= 0:
        raise EvidencePolicyError(
            "evidence_coverage_invalid",
            "expected cadence must be positive",
        )

    usable = tuple(
        observation
        for observation in observations
        if observation.quality.quality_class is not EvidenceQualityClass.INVALID
        and (
            (
                observation.observed_time is not None
                and window.contains(observation.observed_time)
            )
            or (
                observation.valid_time_range is not None
                and observation.valid_time_range.overlaps(window)
            )
        )
    )
    observed_count = len(usable)
    point_times = tuple(
        observation.observed_time
        for observation in usable
        if observation.observed_time is not None
    )
    interval_ranges = tuple(
        observation.valid_time_range
        for observation in usable
        if observation.valid_time_range is not None
    )

    expected_count: int | None = None
    ratio: Decimal | None = None
    covered_seconds = 0
    largest_gap: int | None = None
    status = CoverageStatus.INSUFFICIENT

    if profile.policy_kind is CoveragePolicyKind.CONTINUOUS_REGULAR:
        if expected_cadence_seconds is not None:
            effective_count = len(set(point_times))
            expected_count = int(
                (Decimal(window_seconds) / Decimal(expected_cadence_seconds)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            ratio = min(
                Decimal("1"),
                Decimal(effective_count) / Decimal(expected_count),
            )
            covered_seconds = min(
                window_seconds,
                effective_count * expected_cadence_seconds,
            )
            largest_gap = _largest_point_gap(point_times, window)
            if effective_count >= profile.minimum_usable_count:
                if (
                    profile.complete_ratio is not None
                    and ratio >= Decimal(profile.complete_ratio)
                    and largest_gap is not None
                    and profile.max_gap_cadence_multiplier is not None
                    and Decimal(largest_gap)
                    <= Decimal(expected_cadence_seconds)
                    * Decimal(profile.max_gap_cadence_multiplier)
                ):
                    status = CoverageStatus.COMPLETE
                elif (
                    profile.partial_ratio is not None
                    and ratio >= Decimal(profile.partial_ratio)
                ):
                    status = CoverageStatus.PARTIAL
        elif observed_count:
            status = CoverageStatus.INSUFFICIENT
            largest_gap = _largest_point_gap(point_times, window)
    elif profile.policy_kind is CoveragePolicyKind.CONTINUOUS_LATEST:
        largest_gap = _largest_point_gap(point_times, window)
        if observed_count >= profile.minimum_usable_count:
            if expected_cadence_seconds is None:
                status = CoverageStatus.UNKNOWN
            else:
                latest = max(point_times)
                age_seconds = _seconds(latest, window.end)
                covered_seconds = min(window_seconds, expected_cadence_seconds)
                if (
                    profile.max_gap_cadence_multiplier is not None
                    and Decimal(age_seconds)
                    <= Decimal(expected_cadence_seconds)
                    * Decimal(profile.max_gap_cadence_multiplier)
                ):
                    status = CoverageStatus.COMPLETE
                else:
                    status = CoverageStatus.PARTIAL
    elif profile.policy_kind is CoveragePolicyKind.DISCRETE_SAMPLE:
        if observed_count >= profile.minimum_usable_count:
            status = CoverageStatus.PARTIAL
    else:
        covered_seconds = _union_duration(interval_ranges, window)
        ratio = Decimal(covered_seconds) / Decimal(window_seconds)
        if observed_count >= profile.minimum_usable_count:
            if (
                profile.complete_ratio is not None
                and ratio >= Decimal(profile.complete_ratio)
            ):
                status = CoverageStatus.COMPLETE
            elif (
                profile.partial_ratio is not None
                and ratio >= Decimal(profile.partial_ratio)
            ):
                status = CoverageStatus.PARTIAL

    if not collection_complete:
        status = CoverageStatus.UNKNOWN

    return EvidenceCoverage(
        status=status,
        observed_count=observed_count,
        expected_count=expected_count,
        ratio=(normalize_canonical_decimal(ratio) if ratio is not None else None),
        covered_duration_seconds=covered_seconds,
        window_duration_seconds=window_seconds,
        largest_gap_seconds=largest_gap,
    )
