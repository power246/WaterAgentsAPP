"""M9 domain contract."""

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey


class UnitPolicyChange(DomainModel):
    parameter: ParameterKey
    previous_unit_id: NonEmptyString | None
    target_unit_id: NonEmptyString
    dimension_id: NonEmptyString
