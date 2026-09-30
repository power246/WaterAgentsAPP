from pydantic import field_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class ProviderQuality(DomainModel):
    approval: NonEmptyString | None = None
    qualifiers: tuple[NonEmptyString, ...] = ()

    @field_validator("qualifiers")
    @classmethod
    def validate_qualifiers(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="provider quality qualifiers")
