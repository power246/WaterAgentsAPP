from typing import Annotated

from pydantic import Field, StrictInt, field_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class RAGFailedJobRetryFilter(DomainModel):
    safe_error_codes: tuple[NonEmptyString, ...]
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 25

    @field_validator("safe_error_codes")
    @classmethod
    def validate_safe_error_codes(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        ensure_unique_tuple(values, field_name="safe_error_codes")
        if not values:
            raise ValueError("safe_error_codes must not be empty")
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
        if any(
            len(value) > 64
            or not value[0].isalpha()
            or value.casefold() != value
            or any(character not in allowed for character in value)
            for value in values
        ):
            raise ValueError("safe_error_codes must contain bounded lowercase tokens")
        return values
