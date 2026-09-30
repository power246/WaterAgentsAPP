from datetime import datetime, timedelta, timezone

import pytest

from app.application.evidence_policy import (
    EvidencePolicyError,
    PolicyObservation,
    QualityAssessment,
    aggregate_observations,
    calculate_coverage,
)
from app.domain.evidence import (
    CoverageStatus,
    DataQualityFlag,
    EvidenceQualityClass,
    SourceRecordReference,
)
from app.domain.hydrology import (
    EvidenceAggregationMethod,
    ParameterKey,
    ParameterProvider,
)
from app.domain.time import TimeRange
from app.infrastructure.config import load_parameter_capability_registry


REGISTRY = load_parameter_capability_registry()
START = datetime(2026, 9, 29, tzinfo=timezone.utc)


def capability(code: str):
    result = REGISTRY.get(ParameterKey(code_system="usgs", code=code))
    assert result is not None
    return result


def quality(
    quality_class: EvidenceQualityClass = EvidenceQualityClass.VALID,
    flags: tuple[DataQualityFlag, ...] = (),
) -> QualityAssessment:
    return QualityAssessment(
        quality_class=quality_class,
        flags=flags,
        approval="final",
        qualifiers=(),
    )


def observation(
    code: str,
    index: int,
    value: str,
    *,
    provider: ParameterProvider = ParameterProvider.WDFN_TIME_SERIES,
    channel: str = "continuous",
    observed_time: datetime | None = None,
    valid_time_range: TimeRange | None = None,
    assessment: QualityAssessment | None = None,
    station_id: str = "USGS-001",
    method: str | None = None,
    statistic: str | None = None,
    medium: str | None = None,
    fraction: str | None = None,
) -> PolicyObservation:
    current = capability(code)
    policy = REGISTRY.get_channel_policy(current.key, provider, channel)
    assert policy is not None
    return PolicyObservation(
        record_ref=SourceRecordReference(
            item_id="item-source",
            record_id=f"record-{index:03d}",
        ),
        parameter=current.key,
        provider=provider,
        channel=channel,
        location_comid="12345678",
        station_id=station_id,
        medium=policy.medium_constraint if medium is None else medium,
        fraction=policy.fraction_constraint if fraction is None else fraction,
        method=method or policy.accepted_methods[0],
        statistic=statistic or policy.accepted_statistics[0],
        value_semantics=policy.value_semantics,
        observed_time=observed_time,
        valid_time_range=valid_time_range,
        value=value,
        unit=current.canonical_unit,
        quality=assessment or quality(),
        series_identity=f"series:{code}:{provider.value}:{channel}",
    )


def point_series(
    code: str,
    values: tuple[str, ...],
    *,
    channel: str = "continuous",
) -> tuple[PolicyObservation, ...]:
    return tuple(
        observation(
            code,
            index,
            value,
            channel=channel,
            observed_time=START + timedelta(hours=index),
        )
        for index, value in enumerate(values)
    )


def test_continuous_coverage_uses_declared_cadence_ratio_and_gap() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=6))
    complete = calculate_coverage(
        REGISTRY,
        capability("00060"),
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
        point_series("00060", ("1", "2", "3", "4", "5")),
        window,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )
    partial = calculate_coverage(
        REGISTRY,
        capability("00060"),
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
        point_series("00060", ("1", "2", "3")),
        window,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )

    assert complete.status is CoverageStatus.COMPLETE
    assert complete.expected_count == 6
    assert complete.ratio == "0.8333333333333333333333333333"
    assert complete.largest_gap_seconds == 7200
    assert partial.status is CoverageStatus.PARTIAL
    assert partial.ratio == "0.5"


def test_collection_failure_is_unknown_not_partial_coverage() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=2))
    result = calculate_coverage(
        REGISTRY,
        capability("00060"),
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
        point_series("00060", ("1", "2")),
        window,
        collection_complete=False,
        expected_cadence_seconds=3600,
    )

    assert result.status is CoverageStatus.UNKNOWN


def test_discrete_and_interval_profiles_do_not_invent_cadence() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=2))
    sample = observation(
        "00300",
        0,
        "8",
        provider=ParameterProvider.USGS_SAMPLES,
        channel="discrete_samples",
        observed_time=START + timedelta(minutes=15),
    )
    intervals = tuple(
        observation(
            "00045",
            index,
            str(index + 1),
            channel="daily",
            valid_time_range=TimeRange(
                start=START + timedelta(hours=index),
                end=START + timedelta(hours=index + 1),
            ),
        )
        for index in range(2)
    )

    discrete = calculate_coverage(
        REGISTRY,
        capability("00300"),
        ParameterProvider.USGS_SAMPLES,
        "discrete_samples",
        (sample,),
        window,
        collection_complete=True,
    )
    interval = calculate_coverage(
        REGISTRY,
        capability("00045"),
        ParameterProvider.WDFN_TIME_SERIES,
        "daily",
        intervals,
        window,
        collection_complete=True,
    )

    assert discrete.status is CoverageStatus.PARTIAL
    assert discrete.expected_count is None
    assert discrete.ratio is None
    assert interval.status is CoverageStatus.COMPLETE
    assert interval.covered_duration_seconds == 7200
    assert interval.ratio == "1"


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        (EvidenceAggregationMethod.MEAN, "2.5"),
        (EvidenceAggregationMethod.MEDIAN, "2.5"),
        (EvidenceAggregationMethod.MIN, "1"),
        (EvidenceAggregationMethod.MAX, "4"),
        (EvidenceAggregationMethod.P90, "4"),
        (EvidenceAggregationMethod.LATEST, "4"),
    ],
)
def test_stable_decimal_algorithms(method, expected: str) -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=4))
    result = aggregate_observations(
        REGISTRY,
        capability("00060"),
        point_series("00060", ("1", "2", "3", "4")),
        window,
        method,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )

    assert result.payload.value == expected
    assert result.quality_class is EvidenceQualityClass.VALID


def test_suspicious_inputs_and_partial_coverage_propagate_without_invalid_values() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=3))
    observations = (
        observation("00060", 0, "1", observed_time=START),
        observation(
            "00060",
            1,
            "3",
            observed_time=START + timedelta(hours=1),
            assessment=quality(
                EvidenceQualityClass.SUSPICIOUS,
                (DataQualityFlag.ESTIMATED,),
            ),
        ),
        observation(
            "00060",
            2,
            "100",
            observed_time=START + timedelta(hours=2),
            assessment=quality(
                EvidenceQualityClass.INVALID,
                (DataQualityFlag.PROVIDER_FLAGGED,),
            ),
        ),
    )
    result = aggregate_observations(
        REGISTRY,
        capability("00060"),
        observations,
        window,
        EvidenceAggregationMethod.MEAN,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )

    assert result.payload.value == "2"
    assert result.payload.valid_input_count == 1
    assert result.payload.suspicious_input_count == 1
    assert result.payload.invalid_input_count == 1
    assert result.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert set(result.quality_flags) == {
        DataQualityFlag.ESTIMATED,
        DataQualityFlag.PARTIAL_COVERAGE,
    }


def test_counter_delta_sum_is_ordered_and_marks_resets() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=4))
    observations = point_series("00045", ("10", "15", "3", "8"))
    result = aggregate_observations(
        REGISTRY,
        capability("00045"),
        tuple(reversed(observations)),
        window,
        EvidenceAggregationMethod.COUNTER_DELTA_SUM,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )

    assert result.payload.value == "13"
    assert result.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert DataQualityFlag.COUNTER_RESET in result.quality_flags
    assert result.payload.input_record_refs == tuple(
        item.record_ref for item in observations
    )


def test_interval_total_sum_requires_complete_non_overlapping_intervals() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=2))
    non_overlapping = tuple(
        observation(
            "00045",
            index,
            str(index + 1),
            channel="daily",
            valid_time_range=TimeRange(
                start=START + timedelta(hours=index),
                end=START + timedelta(hours=index + 1),
            ),
        )
        for index in range(2)
    )
    result = aggregate_observations(
        REGISTRY,
        capability("00045"),
        non_overlapping,
        window,
        EvidenceAggregationMethod.SUM,
        collection_complete=True,
    )
    overlapping = (
        observation(
            "00045",
            0,
            "1",
            channel="daily",
            valid_time_range=TimeRange(
                start=START,
                end=START + timedelta(hours=1, minutes=30),
            ),
        ),
        observation(
            "00045",
            1,
            "2",
            channel="daily",
            valid_time_range=TimeRange(
                start=START + timedelta(hours=1),
                end=START + timedelta(hours=2),
            ),
        ),
    )

    assert result.payload.value == "3"
    with pytest.raises(EvidencePolicyError) as error:
        aggregate_observations(
            REGISTRY,
            capability("00045"),
            overlapping,
            window,
            EvidenceAggregationMethod.SUM,
            collection_complete=True,
        )
    assert error.value.code == "evidence_aggregation_blocked"


@pytest.mark.parametrize(
    ("code", "provider", "channel"),
    [
        ("00060", ParameterProvider.WDFN_TIME_SERIES, "continuous"),
        ("00400", ParameterProvider.WDFN_TIME_SERIES, "continuous"),
        ("00300", ParameterProvider.WDFN_TIME_SERIES, "continuous"),
        ("31633", ParameterProvider.USGS_SAMPLES, "discrete_samples"),
    ],
)
def test_sum_is_rejected_for_readings_ph_concentration_and_count_density(
    code: str,
    provider: ParameterProvider,
    channel: str,
) -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=1))
    item = observation(
        code,
        0,
        "1",
        provider=provider,
        channel=channel,
        observed_time=START,
    )

    with pytest.raises(EvidencePolicyError) as error:
        aggregate_observations(
            REGISTRY,
            capability(code),
            (item,),
            window,
            EvidenceAggregationMethod.SUM,
            collection_complete=True,
            expected_cadence_seconds=3600,
        )

    assert error.value.code == "evidence_aggregation_blocked"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("station_id", "USGS-OTHER"),
        ("method", "laboratory"),
        ("fraction", "Filtered"),
        ("statistic", "maximum"),
        ("series_identity", "series:other"),
    ],
)
def test_cross_series_aggregation_is_rejected(field: str, value: str) -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=2))
    first, second = point_series("00060", ("1", "2"))
    changed = second.model_copy(update={field: value})

    with pytest.raises(EvidencePolicyError) as error:
        aggregate_observations(
            REGISTRY,
            capability("00060"),
            (first, changed),
            window,
            EvidenceAggregationMethod.MEAN,
            collection_complete=True,
            expected_cadence_seconds=3600,
        )

    assert error.value.code == "evidence_aggregation_blocked"


def test_latest_tie_incomplete_collection_and_insufficient_coverage_are_blocked() -> None:
    one_hour = TimeRange(start=START, end=START + timedelta(hours=1))
    first = observation("00060", 0, "1", observed_time=START)
    second = observation("00060", 1, "2", observed_time=START)
    with pytest.raises(EvidencePolicyError):
        aggregate_observations(
            REGISTRY,
            capability("00060"),
            (first, second),
            one_hour,
            EvidenceAggregationMethod.LATEST,
            collection_complete=True,
            expected_cadence_seconds=3600,
        )

    with pytest.raises(EvidencePolicyError) as incomplete:
        aggregate_observations(
            REGISTRY,
            capability("00060"),
            (first,),
            one_hour,
            EvidenceAggregationMethod.MEAN,
            collection_complete=False,
            expected_cadence_seconds=3600,
        )
    assert incomplete.value.code == "evidence_collection_incomplete"

    six_hours = TimeRange(start=START, end=START + timedelta(hours=6))
    with pytest.raises(EvidencePolicyError) as insufficient:
        aggregate_observations(
            REGISTRY,
            capability("00060"),
            (first,),
            six_hours,
            EvidenceAggregationMethod.MEAN,
            collection_complete=True,
            expected_cadence_seconds=3600,
        )
    assert insufficient.value.code == "evidence_coverage_insufficient"


def test_input_order_does_not_change_aggregate_bytes() -> None:
    window = TimeRange(start=START, end=START + timedelta(hours=4))
    observations = point_series("00060", ("1", "2", "3", "4"))
    first = aggregate_observations(
        REGISTRY,
        capability("00060"),
        observations,
        window,
        EvidenceAggregationMethod.MEAN,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )
    second = aggregate_observations(
        REGISTRY,
        capability("00060"),
        tuple(reversed(observations)),
        window,
        EvidenceAggregationMethod.MEAN,
        collection_complete=True,
        expected_cadence_seconds=3600,
    )

    assert first.model_dump_json() == second.model_dump_json()
