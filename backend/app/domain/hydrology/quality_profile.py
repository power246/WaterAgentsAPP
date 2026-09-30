from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class QualityProfile(DomainModel):
    profile_id: NonEmptyString
    extends_profile_id: NonEmptyString | None = None
    valid_tokens: tuple[NonEmptyString, ...]
    invalid_tokens: tuple[NonEmptyString, ...]
    suspicious_tokens: tuple[NonEmptyString, ...]
    unknown_token_class: Literal["suspicious"]

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        for field_name in (
            "valid_tokens",
            "invalid_tokens",
            "suspicious_tokens",
        ):
            ensure_unique_tuple(getattr(self, field_name), field_name=field_name)
        classified = (
            set(self.valid_tokens),
            set(self.invalid_tokens),
            set(self.suspicious_tokens),
        )
        if any(
            left & right
            for index, left in enumerate(classified)
            for right in classified[index + 1 :]
        ):
            raise ValueError("quality tokens must not have conflicting classes")
        return self
