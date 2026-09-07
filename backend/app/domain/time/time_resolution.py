from app.domain.common import DomainEnum


class TimeResolution(DomainEnum):
    SIX_HOURS = "6h"
    TWELVE_HOURS = "12h"
    TWENTY_FOUR_HOURS = "24h"

    @property
    def hours(self) -> int:
        return int(self.value.removesuffix("h"))


DEFAULT_TIME_RESOLUTION = TimeResolution.TWELVE_HOURS
