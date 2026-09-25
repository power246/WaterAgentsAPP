from app.domain.common import DomainEnum


class TimeResolutionPreference(DomainEnum):
    AUTO = "auto"
    SIX_HOURS = "6h"
    TWELVE_HOURS = "12h"
    TWENTY_FOUR_HOURS = "24h"


DEFAULT_TIME_RESOLUTION_PREFERENCE = TimeResolutionPreference.TWELVE_HOURS
