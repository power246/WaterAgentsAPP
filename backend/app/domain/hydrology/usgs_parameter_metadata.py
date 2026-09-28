from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey


class UsgsParameterMetadata(DomainModel):
    key: ParameterKey
    name: NonEmptyString
    unit: NonEmptyString
    provider_identity: NonEmptyString
