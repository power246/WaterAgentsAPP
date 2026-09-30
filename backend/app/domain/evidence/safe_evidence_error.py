from app.domain.common import DomainModel, NonEmptyString


class SafeEvidenceError(DomainModel):
    code: NonEmptyString
    message: NonEmptyString
    retryable: bool
