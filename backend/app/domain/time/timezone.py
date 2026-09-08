from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_iana_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError(
            "analysis_timezone must be a valid IANA timezone name"
        ) from error
