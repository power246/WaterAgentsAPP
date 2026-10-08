"""M9 domain contract."""

from typing import Self
from pydantic import StrictBool, model_validator
from app.domain.common import DomainModel, NonEmptyString
from app.domain.model_role import ModelRole
from app.domain.model_selection import ModelSelection


class ModelOption(DomainModel):
    selection: ModelSelection
    label: NonEmptyString
    roles: tuple[ModelRole, ...]
    available: StrictBool
    unavailable_reason: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_option(self) -> Self:
        if not self.roles or len(self.roles) != len(set(self.roles)):
            raise ValueError("model option roles must be nonempty and unique")
        if ModelRole.EMBEDDING in self.roles:
            raise ValueError("embedding capability belongs in embedding_profiles")
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("availability must match unavailable_reason")
        return self
