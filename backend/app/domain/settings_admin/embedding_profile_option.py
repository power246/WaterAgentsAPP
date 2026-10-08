"""M9 domain contract."""

from typing import Self
from pydantic import StrictBool, model_validator
from app.domain.common import DomainModel, NonEmptyString
from .primitives import ProfileId


class EmbeddingProfileOption(DomainModel):
    profile_id: ProfileId
    label: NonEmptyString
    available: StrictBool
    unavailable_reason: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_option(self) -> Self:
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("availability must match unavailable_reason")
        return self
