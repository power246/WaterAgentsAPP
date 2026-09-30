from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class QualityProfile(DomainModel):
    profile_id: NonEmptyString
    extends_profile_id: NonEmptyString | None = None
    invalid_tokens: tuple[NonEmptyString, ...]
    suspicious_tokens: tuple[NonEmptyString, ...]
    unknown_token_class: Literal["suspicious"]

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        ensure_unique_tuple(self.invalid_tokens, field_name="invalid_tokens")
        ensure_unique_tuple(self.suspicious_tokens, field_name="suspicious_tokens")
        if set(self.invalid_tokens) & set(self.suspicious_tokens):
            raise ValueError("quality tokens must not have conflicting classes")
        return self
