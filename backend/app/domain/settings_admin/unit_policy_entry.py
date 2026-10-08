"""M9 domain contract."""

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey


class UnitPolicyEntry(DomainModel):
    parameter: ParameterKey
    target_unit_id: NonEmptyString
