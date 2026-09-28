from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.application.query_understanding import (
    DateResolutionStatus,
    DateResolver,
    ParameterConcept,
    ParsedIntentV1,
)
from app.application.transcripts.models import AnalysisDateRangeV1
from app.domain import (
    InferenceMode,
    TimeResolution,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TimeResolutionSource,
    build_time_windows,
)


DECISION = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def parsed(
    start: date | None = None,
    end: date | None = None,
) -> ParsedIntentV1:
    return ParsedIntentV1(
        start_date=start,
        end_date=end,
        parameters=(
            ParameterConcept(
                concept="pH",
                rationale="pH was requested.",
                explicitly_named=True,
            ),
        ),
    )


def resolve(
    *,
    frontend: AnalysisDateRangeV1 | None = None,
    intent: ParsedIntentV1 | None = None,
    decision: datetime = DECISION,
    timezone_name: str = "America/New_York",
    preference: TimeResolutionPreference = TimeResolutionPreference.AUTO,
    preference_source: TimeResolutionPreferenceSource = (
        TimeResolutionPreferenceSource.FRONTEND
    ),
):
    return DateResolver().resolve(
        frontend,
        intent or parsed(),
        decision_instant=decision,
        analysis_timezone=timezone_name,
        resolution_preference=preference,
        resolution_preference_source=preference_source,
    )


def test_frontend_only_query_only_and_same_dates_resolve_identically() -> None:
    date_range = AnalysisDateRangeV1(
        start_date=date(2026, 9, 26),
        end_date=date(2026, 9, 28),
    )

    frontend_only = resolve(frontend=date_range)
    query_only = resolve(intent=parsed(date_range.start_date, date_range.end_date))
    both = resolve(
        frontend=date_range,
        intent=parsed(date_range.start_date, date_range.end_date),
    )

    assert frontend_only == query_only == both
    assert frontend_only.status is DateResolutionStatus.SUCCESS


def test_semantically_different_frontend_and_query_dates_conflict() -> None:
    result = resolve(
        frontend=AnalysisDateRangeV1(
            start_date=date(2026, 9, 26),
            end_date=date(2026, 9, 28),
        ),
        intent=parsed(date(2026, 9, 27), date(2026, 9, 28)),
    )

    assert result.status is DateResolutionStatus.DATE_RANGE_CONFLICT
    assert result.temporal is None


def test_missing_dates_require_a_range_and_half_range_is_invalid_parser_output() -> None:
    assert resolve().status is DateResolutionStatus.DATE_RANGE_REQUIRED

    with pytest.raises(ValidationError, match="must be provided together"):
        parsed(date(2026, 9, 25), None)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 9, 18), date(2026, 9, 18), DateResolutionStatus.SUCCESS),
        (date(2026, 10, 2), date(2026, 10, 2), DateResolutionStatus.SUCCESS),
        (
            date(2026, 9, 17),
            date(2026, 9, 18),
            DateResolutionStatus.DATE_RANGE_OUT_OF_BOUNDS,
        ),
        (
            date(2026, 10, 2),
            date(2026, 10, 3),
            DateResolutionStatus.DATE_RANGE_OUT_OF_BOUNDS,
        ),
        (
            date(2026, 9, 26),
            date(2026, 9, 25),
            DateResolutionStatus.DATE_RANGE_OUT_OF_BOUNDS,
        ),
    ],
)
def test_local_today_plus_or_minus_seven_is_a_closed_interval(
    start: date,
    end: date,
    expected: DateResolutionStatus,
) -> None:
    assert resolve(intent=parsed(start, end)).status is expected


def test_bounds_cross_year_and_month_without_using_utc_date() -> None:
    decision = datetime(2027, 1, 1, 1, 0, tzinfo=timezone.utc)

    result = resolve(
        intent=parsed(date(2026, 12, 24), date(2027, 1, 7)),
        decision=decision,
    )

    assert result.status is DateResolutionStatus.SUCCESS
    assert result.temporal is not None
    assert result.temporal.requested_date_range.calendar_days == 15


@pytest.mark.parametrize(
    ("decision", "start", "end", "expected_start", "expected_end"),
    [
        (
            datetime(2026, 3, 8, 12, tzinfo=timezone.utc),
            date(2026, 3, 7),
            date(2026, 3, 8),
            datetime(2026, 3, 7, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
            date(2026, 10, 31),
            date(2026, 11, 1),
            datetime(2026, 10, 31, 4, tzinfo=timezone.utc),
            datetime(2026, 11, 2, 5, tzinfo=timezone.utc),
        ),
    ],
)
def test_inclusive_local_end_date_converts_to_dst_safe_exclusive_utc_end(
    decision: datetime,
    start: date,
    end: date,
    expected_start: datetime,
    expected_end: datetime,
) -> None:
    result = resolve(intent=parsed(start, end), decision=decision)

    assert result.temporal is not None
    assert result.temporal.time_range.start == expected_start
    assert result.temporal.time_range.end == expected_end


@pytest.mark.parametrize(
    ("start", "end", "mode", "segment_modes"),
    [
        (
            date(2026, 9, 18),
            date(2026, 9, 24),
            InferenceMode.GAP_FILLING,
            (InferenceMode.GAP_FILLING,),
        ),
        (
            date(2026, 9, 25),
            date(2026, 10, 1),
            InferenceMode.PREDICTION,
            (InferenceMode.PREDICTION,),
        ),
        (
            date(2026, 9, 24),
            date(2026, 9, 25),
            InferenceMode.MIXED,
            (InferenceMode.GAP_FILLING, InferenceMode.PREDICTION),
        ),
    ],
)
def test_mode_and_segments_are_derived_at_local_today_midnight(
    start: date,
    end: date,
    mode: InferenceMode,
    segment_modes: tuple[InferenceMode, ...],
) -> None:
    result = resolve(intent=parsed(start, end))

    assert result.temporal is not None
    assert result.temporal.mode is mode
    assert tuple(item.mode for item in result.temporal.time_segments) == segment_modes
    if mode is InferenceMode.MIXED:
        assert result.temporal.time_segments[0].time_range.end == datetime(
            2026, 9, 25, 4, tzinfo=timezone.utc
        )
        assert (
            result.temporal.time_segments[0].time_range.end
            == result.temporal.time_segments[1].time_range.start
        )


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, TimeResolution.SIX_HOURS),
        (7, TimeResolution.SIX_HOURS),
        (8, TimeResolution.TWELVE_HOURS),
        (15, TimeResolution.TWELVE_HOURS),
    ],
)
def test_auto_resolution_uses_inclusive_calendar_day_count(
    days: int,
    expected: TimeResolution,
) -> None:
    start = date(2026, 9, 18)
    end = start.fromordinal(start.toordinal() + days - 1)

    result = resolve(intent=parsed(start, end))

    assert result.temporal is not None
    assert result.temporal.time_resolution is expected
    assert result.temporal.time_resolution_source is TimeResolutionSource.AUTO


def test_omitted_api_resolution_is_12h_default() -> None:
    result = resolve(
        intent=parsed(date(2026, 9, 25), date(2026, 9, 25)),
        preference=TimeResolutionPreference.TWELVE_HOURS,
        preference_source=TimeResolutionPreferenceSource.DEFAULT,
    )

    assert result.temporal is not None
    assert result.temporal.time_resolution is TimeResolution.TWELVE_HOURS
    assert result.temporal.time_resolution_source is TimeResolutionSource.DEFAULT


def test_24h_is_preserved_only_as_an_explicit_frontend_resolution() -> None:
    result = resolve(
        intent=parsed(date(2026, 9, 25), date(2026, 9, 25)),
        preference=TimeResolutionPreference.TWENTY_FOUR_HOURS,
        preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )

    assert result.temporal is not None
    assert result.temporal.time_resolution is TimeResolution.TWENTY_FOUR_HOURS
    assert result.temporal.time_resolution_source is TimeResolutionSource.FRONTEND


def test_explicit_6h_on_full_15_day_range_has_exactly_60_windows() -> None:
    result = resolve(
        intent=parsed(date(2026, 9, 18), date(2026, 10, 2)),
        preference=TimeResolutionPreference.SIX_HOURS,
        preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )

    assert result.temporal is not None
    assert len(
        build_time_windows(
            result.temporal.time_range,
            result.temporal.time_resolution,
            "America/New_York",
        )
    ) == 60
