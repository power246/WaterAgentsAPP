from app.domain.common import NonEmptyString
from app.domain.domain_model import DomainModel


class ModelSelection(DomainModel):
    provider: NonEmptyString
    model: NonEmptyString
