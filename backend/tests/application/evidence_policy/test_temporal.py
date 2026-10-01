from datetime import UTC, datetime

import pytest

from app.application.evidence_policy import temporal_relation_matches
from app.application.query_understanding import EvidenceTemporalRelation
from app.domain import TimeRange
from app.domain.time.timezone import get_iana_timezone


def local_range(
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> TimeRange:
    zone = get_iana_timezone(timezone_name)
    return TimeRange(
        start=start.replace(tzinfo=zone).astimezone(UTC),
        end=end.replace(tzinfo=zone).astimezone(UTC),
    )


@pytest.mark.parametrize(
    ("target_start", "target_end", "source_start", "source_end"),
    (
        (
            datetime(2026, 3, 9, 23),
            datetime(2026, 3, 9, 23, 30),
            datetime(2026, 3, 8, 0),
            datetime(2026, 3, 8, 0, 30),
        ),
        (
            datetime(2026, 11, 1, 0),
            datetime(2026, 11, 1, 0, 30),
            datetime(2026, 11, 2, 23),
            datetime(2026, 11, 2, 23, 30),
        ),
    ),
)
def test_adjacent_local_day_accepts_neighboring_dates_across_dst(
    target_start: datetime,
    target_end: datetime,
    source_start: datetime,
    source_end: datetime,
) -> None:
    timezone_name = "America/New_York"
    assert temporal_relation_matches(
        EvidenceTemporalRelation.ADJACENT_LOCAL_DAY,
        local_range(source_start, source_end, timezone_name),
        local_range(target_start, target_end, timezone_name),
        analysis_timezone=timezone_name,
    )


def test_adjacent_local_day_rejects_same_date_and_non_adjacent_dates() -> None:
    timezone_name = "America/New_York"
    target = local_range(
        datetime(2026, 11, 1, 0),
        datetime(2026, 11, 1, 6),
        timezone_name,
    )
    same_date = local_range(
        datetime(2026, 11, 1, 12),
        datetime(2026, 11, 1, 18),
        timezone_name,
    )
    two_days_later = local_range(
        datetime(2026, 11, 3, 0),
        datetime(2026, 11, 3, 6),
        timezone_name,
    )

    assert not temporal_relation_matches(
        EvidenceTemporalRelation.ADJACENT_LOCAL_DAY,
        same_date,
        target,
        analysis_timezone=timezone_name,
    )
    assert not temporal_relation_matches(
        EvidenceTemporalRelation.ADJACENT_LOCAL_DAY,
        two_days_later,
        target,
        analysis_timezone=timezone_name,
    )
