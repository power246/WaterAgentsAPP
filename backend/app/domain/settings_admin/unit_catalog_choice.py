"""Safe target unit offered by the current registry."""

from app.domain.common import DomainModel, NonEmptyString


class UnitCatalogChoice(DomainModel):
    unit_id: NonEmptyString
    symbol: NonEmptyString
    dimension_id: NonEmptyString
