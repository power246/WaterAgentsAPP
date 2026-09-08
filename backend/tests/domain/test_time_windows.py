from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.domain import TimeRange, TimeResolution, build_time_windows


def local_time_range(
    timezone_name: str,
    start: datetime,
    end: datetime,
) -> TimeRange:
    zone = ZoneInfo(timezone_name)
    return TimeRange(start=start.replace(tzinfo=zone), end=end.replace(tzinfo=zone))


def local_bounds(
    windows: tuple[TimeRange, ...],
    timezone_name: str,
) -> list[tuple[datetime, datetime]]:
    zone = ZoneInfo(timezone_name)
    return [
        (window.start.astimezone(zone), window.end.astimezone(zone))
        for window in windows
    ]


def assert_contiguous_coverage(
    windows: tuple[TimeRange, ...],
    requested: TimeRange,
) -> None:
    assert windows
    assert windows[0].start == requested.start
    assert windows[-1].end == requested.end
    assert all(window.start < window.end for window in windows)
    assert all(
        current.end == following.start
        for current, following in zip(windows, windows[1:])
    )


@pytest.mark.parametrize(
    ("resolution", "expected_hours"),
    [
        (TimeResolution.SIX_HOURS, [(0, 6), (6, 12), (12, 18), (18, 0)]),
        (TimeResolution.TWELVE_HOURS, [(0, 12), (12, 0)]),
        (TimeResolution.TWENTY_FOUR_HOURS, [(0, 0)]),
    ],
)
def test_regular_day_uses_local_wall_clock_boundaries(
    resolution: TimeResolution,
    expected_hours: list[tuple[int, int]],
) -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 2, 1),
        datetime(2026, 2, 2),
    )

    windows = build_time_windows(requested, resolution, timezone_name)

    assert [
        (start.hour, end.hour) for start, end in local_bounds(windows, timezone_name)
    ] == expected_hours
    assert_contiguous_coverage(windows, requested)


@pytest.mark.parametrize(
    ("date", "six_hour_elapsed", "day_elapsed"),
    [
        (datetime(2026, 3, 8), timedelta(hours=5), timedelta(hours=23)),
        (datetime(2026, 11, 1), timedelta(hours=7), timedelta(hours=25)),
    ],
)
def test_chicago_dst_changes_elapsed_time_only_when_offset_changes(
    date: datetime,
    six_hour_elapsed: timedelta,
    day_elapsed: timedelta,
) -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(timezone_name, date, date + timedelta(days=1))

    six_hour_windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )
    day_windows = build_time_windows(
        requested,
        TimeResolution.TWENTY_FOUR_HOURS,
        timezone_name,
    )

    assert six_hour_windows[0].end - six_hour_windows[0].start == six_hour_elapsed
    assert day_windows[0].end - day_windows[0].start == day_elapsed
    assert_contiguous_coverage(six_hour_windows, requested)
    assert_contiguous_coverage(day_windows, requested)


def test_repeated_fall_back_wall_time_has_distinct_utc_keys() -> None:
    zone = ZoneInfo("America/Chicago")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)

    first_utc = first.astimezone(timezone.utc)
    second_utc = second.astimezone(timezone.utc)
    keyed_by_instant = {first_utc: "first", second_utc: "second"}

    assert first_utc != second_utc
    assert len(keyed_by_instant) == 2


def test_regular_day_does_not_treat_five_or_seven_hours_as_full_six_hours() -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 2, 1),
        datetime(2026, 2, 2),
    )

    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )

    assert {window.end - window.start for window in windows} == {
        timedelta(hours=6)
    }


def test_custom_start_rolls_forward_and_truncates_the_tail() -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 2, 1, 3, 20),
        datetime(2026, 2, 1, 17, 40),
    )

    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )

    assert [
        ((start.hour, start.minute), (end.hour, end.minute))
        for start, end in local_bounds(windows, timezone_name)
    ] == [
        ((3, 20), (9, 20)),
        ((9, 20), (15, 20)),
        ((15, 20), (17, 40)),
    ]
    assert windows[-1].end - windows[-1].start == timedelta(hours=2, minutes=20)
    assert_contiguous_coverage(windows, requested)


def test_custom_start_shifts_nonexistent_boundary_without_grid_drift() -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 3, 7, 20, 30),
        datetime(2026, 3, 8, 15),
    )

    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )

    assert [
        (
            start.day,
            start.hour,
            start.minute,
            end.day,
            end.hour,
            end.minute,
        )
        for start, end in local_bounds(windows, timezone_name)
    ] == [
        (7, 20, 30, 8, 3, 30),
        (8, 3, 30, 8, 8, 30),
        (8, 8, 30, 8, 14, 30),
        (8, 14, 30, 8, 15, 0),
    ]
    assert_contiguous_coverage(windows, requested)


def test_ambiguous_boundary_uses_first_occurrence_without_grid_drift() -> None:
    timezone_name = "America/Chicago"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 10, 31, 19, 30),
        datetime(2026, 11, 1, 14),
    )

    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )
    bounds = local_bounds(windows, timezone_name)

    assert (bounds[0][1].hour, bounds[0][1].minute, bounds[0][1].fold) == (
        1,
        30,
        0,
    )
    assert [(end.hour, end.minute) for _, end in bounds[1:]] == [
        (7, 30),
        (13, 30),
        (14, 0),
    ]
    assert_contiguous_coverage(windows, requested)


def test_non_chicago_iana_timezone_is_used() -> None:
    timezone_name = "Europe/Berlin"
    requested = local_time_range(
        timezone_name,
        datetime(2026, 3, 29),
        datetime(2026, 3, 30),
    )

    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        timezone_name,
    )

    assert windows[0].end - windows[0].start == timedelta(hours=5)
    assert_contiguous_coverage(windows, requested)


@pytest.mark.parametrize("timezone_name", ["", "Not/A_Zone", "Central Standard Time"])
def test_invalid_iana_timezone_is_rejected(timezone_name: str) -> None:
    requested = TimeRange(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="valid IANA timezone"):
        build_time_windows(requested, TimeResolution.SIX_HOURS, timezone_name)
