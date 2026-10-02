from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.value_semantics import ValueSemantics


class ChannelPolicy(DomainModel):
    provider: ParameterProvider
    channel: NonEmptyString
    value_semantics: ValueSemantics
    series_identity_fields: tuple[NonEmptyString, ...]
    accepted_statistics: tuple[NonEmptyString, ...]
    accepted_methods: tuple[NonEmptyString, ...]
    aggregation_profile_id: NonEmptyString
    coverage_profile_id: NonEmptyString
    quality_profile_id: NonEmptyString
    medium_constraint: NonEmptyString | None
    fraction_constraint: NonEmptyString | None
    direct_observation: StrictBool

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        for field_name in (
            "series_identity_fields",
            "accepted_statistics",
            "accepted_methods",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        return self
