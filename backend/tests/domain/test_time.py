from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain import (
    DEFAULT_TIME_RESOLUTION,
    InferenceMode,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    TimeSegment,
)


def test_time_enums_have_stable_values_and_default_resolution() -> None:
    assert [resolution.value for resolution in TimeResolution] == [
        "6h",
        "12h",
        "24h",
    ]
    assert [source.value for source in TimeResolutionSource] == [
        "frontend",
        "default",
        "auto",
    ]
    assert [mode.value for mode in InferenceMode] == [
        "gap_filling",
        "prediction",
        "mixed",
    ]
    assert DEFAULT_TIME_RESOLUTION is TimeResolution.TWELVE_HOURS


def test_time_resolution_source_round_trips() -> None:
    adapter = TypeAdapter(TimeResolutionSource)

    serialized = adapter.dump_json(TimeResolutionSource.FRONTEND)

    assert serialized == b'"frontend"'
    assert adapter.validate_json(serialized) is TimeResolutionSource.FRONTEND


@pytest.mark.parametrize("value", ["1h", "6H", "daily", ""])
def test_time_resolution_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ValueError):
        TimeResolution(value)


def test_time_range_normalizes_to_utc_and_round_trips() -> None:
    central = timezone(timedelta(hours=-6))
    time_range = TimeRange(
        start=datetime(2026, 1, 1, 0, tzinfo=central),
        end=datetime(2026, 1, 1, 6, tzinfo=central),
    )

    assert time_range.start == datetime(2026, 1, 1, 6, tzinfo=timezone.utc)
    assert time_range.end == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert TimeRange.model_validate_json(time_range.model_dump_json()) == time_range


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc)),
        (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2)),
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ],
)
def test_time_range_rejects_naive_equal_and_reversed_bounds(
    start: datetime,
    end: datetime,
) -> None:
    with pytest.raises(ValidationError):
        TimeRange(start=start, end=end)


def test_time_range_contains_uses_half_open_bounds() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    time_range = TimeRange(start=start, end=end)

    assert time_range.contains(start)
    assert time_range.contains(end - timedelta(microseconds=1))
    assert not time_range.contains(end)
    assert not time_range.contains(start - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="timezone"):
        time_range.contains(datetime(2026, 1, 1))


def test_time_range_overlaps_excludes_adjacent_ranges() -> None:
    first = TimeRange(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    overlapping = TimeRange(
        start=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
    )
    adjacent = TimeRange(
        start=first.end,
        end=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    assert first.overlaps(overlapping)
    assert overlapping.overlaps(first)
    assert not first.overlaps(adjacent)


@pytest.mark.parametrize(
    "mode",
    [InferenceMode.GAP_FILLING, InferenceMode.PREDICTION],
)
def test_time_segment_accepts_atomic_modes_and_round_trips(
    mode: InferenceMode,
) -> None:
    segment = TimeSegment(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        mode=mode,
    )

    assert TimeSegment.model_validate_json(segment.model_dump_json()) == segment


def test_time_segment_rejects_mixed_mode() -> None:
    with pytest.raises(ValidationError, match="must not be mixed"):
        TimeSegment(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
            mode=InferenceMode.MIXED,
        )
