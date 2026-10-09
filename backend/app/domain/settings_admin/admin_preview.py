"""M9 domain contract."""

from typing import Self
from pydantic import StrictBool, model_validator
from app.domain.common import DomainModel, NonEmptyString
from .primitives import Digest
from .admin_action_kind import AdminActionKind
from .admin_confirmation import AdminConfirmation


class AdminPreview(DomainModel):
    action: AdminActionKind
    resource_identity: NonEmptyString
    fact_fingerprint: Digest
    executable: StrictBool
    confirmation: AdminConfirmation | None = None

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        if self.executable != (self.confirmation is not None):
            raise ValueError("executable preview requires confirmation")
        return self
