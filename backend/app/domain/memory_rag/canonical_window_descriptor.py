from typing import Annotated

from pydantic import Field, field_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainModel,
    ensure_unique_tuple,
)
from app.domain.inference.mode import InferenceMode
from app.domain.memory_rag.meteorological_season import MeteorologicalSeason
from app.domain.time.time_resolution import TimeResolution


class CanonicalWindowDescriptorV1(DomainModel):
    window_kind: InferenceMode
    duration_days: CanonicalDecimal
    local_months: Annotated[tuple[int, ...], Field(min_length=1, max_length=2)]
    season_buckets: Annotated[
        tuple[MeteorologicalSeason, ...],
        Field(min_length=1, max_length=2),
    ]
    resolution: TimeResolution

    @field_validator("window_kind")
    @classmethod
    def reject_mixed_window(cls, value: InferenceMode) -> InferenceMode:
        if value is InferenceMode.MIXED:
            raise ValueError("canonical windows cannot use mixed mode")
        return value

    @field_validator("local_months")
    @classmethod
    def validate_months(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        ensure_unique_tuple(value, field_name="canonical local months")
        if any(type(month) is not int or not 1 <= month <= 12 for month in value):
            raise ValueError("local months must be integers from 1 through 12")
        return tuple(sorted(value))

    @field_validator("season_buckets")
    @classmethod
    def validate_seasons(
        cls,
        value: tuple[MeteorologicalSeason, ...],
    ) -> tuple[MeteorologicalSeason, ...]:
        ensure_unique_tuple(value, field_name="canonical season buckets")
        order = {season: index for index, season in enumerate(MeteorologicalSeason)}
        return tuple(sorted(value, key=order.__getitem__))
