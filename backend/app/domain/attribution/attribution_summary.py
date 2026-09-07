from app.domain.attribution.attribution_factor import AttributionFactor
from app.domain.common import DomainModel, NonEmptyString


class AttributionSummary(DomainModel):
    summary: NonEmptyString
    primary_factors: tuple[AttributionFactor, ...] = ()
