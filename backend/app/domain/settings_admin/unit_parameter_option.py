"""Target units available for a parameter in the current registry."""

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey

from .unit_catalog_choice import UnitCatalogChoice


class UnitParameterOption(DomainModel):
    parameter: ParameterKey
    name: NonEmptyString
    source_unit_id: NonEmptyString
    target_units: tuple[UnitCatalogChoice, ...]
