from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import TimeResolution


def build_time_windows(
    time_range: TimeRange,
    time_resolution: TimeResolution,
    analysis_timezone: str,
) -> tuple[TimeRange, ...]:
    try:
        zone = ZoneInfo(analysis_timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError(
            "analysis_timezone must be a valid IANA timezone name"
        ) from error

    try:
        resolution = TimeResolution(time_resolution)
    except (TypeError, ValueError) as error:
        raise ValueError("time_resolution must be 6h, 12h, or 24h") from error

    windows: list[TimeRange] = []
    cursor = time_range.start
    wall_clock_step = timedelta(hours=resolution.hours)

    while cursor < time_range.end:
        next_local_boundary = cursor.astimezone(zone) + wall_clock_step
        next_boundary = next_local_boundary.astimezone(timezone.utc)
        window_end = min(next_boundary, time_range.end)
        if window_end <= cursor:
            raise ValueError("time window boundary must advance in UTC")
        windows.append(TimeRange(start=cursor, end=window_end))
        cursor = window_end

    return tuple(windows)
