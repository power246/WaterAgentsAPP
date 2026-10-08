"""M9 domain contract."""

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime


class AdminConfirmation(DomainModel):
    token: NonEmptyString
    expires_at: UtcDatetime
    required_phrase: NonEmptyString
