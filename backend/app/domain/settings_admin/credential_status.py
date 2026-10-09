"""M9 domain contract."""

from typing import Annotated, Self
from pydantic import StrictBool, StringConstraints, model_validator
from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from .credential_name import CredentialName
from .credential_source import CredentialSource


class CredentialStatus(DomainModel):
    key_name: CredentialName
    label: NonEmptyString
    purpose: tuple[NonEmptyString, ...]
    configured: StrictBool
    source: CredentialSource
    masked: Annotated[str, StringConstraints(pattern=r"^••••.{0,4}$")] | None
    updated_at: UtcDatetime | None
    store_available: StrictBool
    can_save: StrictBool
    can_delete_user_override: StrictBool
    can_test: StrictBool

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if not self.purpose or len(self.purpose) != len(set(self.purpose)):
            raise ValueError("credential purpose must be nonempty and unique")
        if self.configured != (self.source is not CredentialSource.NONE):
            raise ValueError("configured must match effective source")
        if not self.configured and (self.masked is not None or self.updated_at is not None):
            raise ValueError("absent credential must not have metadata")
        if self.configured and self.masked is None:
            raise ValueError("configured credential requires a masked value")
        if (self.source is CredentialSource.USER_STORE) != (self.updated_at is not None):
            raise ValueError("only user-store credentials have updated_at")
        if not self.store_available and (self.can_save or self.can_delete_user_override):
            raise ValueError("unavailable store cannot accept mutations")
        if self.can_delete_user_override and self.source is not CredentialSource.USER_STORE:
            raise ValueError("only a user-store override can be deleted")
        if self.can_test and not self.configured:
            raise ValueError("test requires an effective credential")
        return self
