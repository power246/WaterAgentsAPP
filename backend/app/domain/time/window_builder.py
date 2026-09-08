from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import TimeResolution
from app.domain.time.timezone import get_iana_timezone


def _resolve_local_boundary(local_boundary: datetime, zone: ZoneInfo) -> datetime:
    valid_instants: set[datetime] = set()
    forward_instants: list[datetime] = []

    for fold in (0, 1):
        candidate = local_boundary.replace(tzinfo=zone, fold=fold)
        instant = candidate.astimezone(timezone.utc)
        round_trip = instant.astimezone(zone).replace(tzinfo=None)
        if round_trip == local_boundary:
            valid_instants.add(instant)
        elif round_trip > local_boundary:
            forward_instants.append(instant)

    if valid_instants:
        return min(valid_instants)
    if forward_instants:
        return min(
            forward_instants,
            key=lambda instant: instant.astimezone(zone).replace(tzinfo=None),
        )
    raise ValueError("local time window boundary cannot be resolved")


def build_time_windows(
    time_range: TimeRange,
    time_resolution: TimeResolution,
    analysis_timezone: str,
) -> tuple[TimeRange, ...]:
    zone = get_iana_timezone(analysis_timezone)

    try:
        resolution = TimeResolution(time_resolution)
    except (TypeError, ValueError) as error:
        raise ValueError("time_resolution must be 6h, 12h, or 24h") from error

    windows: list[TimeRange] = []
    cursor = time_range.start
    nominal_local_boundary = cursor.astimezone(zone).replace(tzinfo=None)
    wall_clock_step = timedelta(hours=resolution.hours)

    while cursor < time_range.end:
        nominal_local_boundary += wall_clock_step
        next_boundary = _resolve_local_boundary(nominal_local_boundary, zone)
        if next_boundary <= cursor:
            continue
        window_end = min(next_boundary, time_range.end)
        windows.append(TimeRange(start=cursor, end=window_end))
        cursor = window_end

    return tuple(windows)
