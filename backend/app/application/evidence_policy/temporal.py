from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.application.query_understanding import EvidenceTemporalRelation
from app.domain.time import TimeRange
from app.domain.time.timezone import get_iana_timezone


def temporal_relation_matches(
    relation: EvidenceTemporalRelation,
    source: TimeRange,
    target: TimeRange,
    *,
    analysis_timezone: str,
) -> bool:
    if relation is EvidenceTemporalRelation.SAME_WINDOW:
        return source == target
    if relation is EvidenceTemporalRelation.OVERLAPPING_WINDOW:
        return source.overlaps(target)
    if relation is not EvidenceTemporalRelation.ADJACENT_LOCAL_DAY:
        return False

    zone = get_iana_timezone(analysis_timezone)
    source_date = _single_local_date(source, zone)
    target_date = _single_local_date(target, zone)
    if source_date is None or target_date is None:
        return False
    return abs((source_date - target_date).days) == 1


def _single_local_date(value: TimeRange, zone: ZoneInfo) -> date | None:
    start_date = value.start.astimezone(zone).date()
    end_date = (value.end - timedelta(microseconds=1)).astimezone(zone).date()
    return start_date if start_date == end_date else None
