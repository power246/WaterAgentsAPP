from app.domain.common import DomainModel, NonEmptyString


class ParameterKey(DomainModel):
    code_system: NonEmptyString
    code: NonEmptyString
