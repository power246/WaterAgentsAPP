from decimal import Decimal, ROUND_HALF_EVEN
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.mode import InferenceMode
from app.domain.memory_rag.canonical_window_descriptor import (
    CanonicalWindowDescriptorV1,
)
from app.domain.memory_rag.meteorological_season import MeteorologicalSeason
from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import TimeResolution


class RAGCaseWindowV1(DomainModel):
    window_id: NonEmptyString
    time_range: TimeRange
    window_kind: InferenceMode
    local_months: Annotated[tuple[int, ...], Field(min_length=1, max_length=2)]
    season_buckets: Annotated[
        tuple[MeteorologicalSeason, ...],
        Field(min_length=1, max_length=2),
    ]
    season_year: int = Field(strict=True)
    duration_seconds: int = Field(strict=True, gt=0)
    resolution: TimeResolution
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]

    @field_validator("window_kind")
    @classmethod
    def reject_mixed_window(cls, value: InferenceMode) -> InferenceMode:
        if value is InferenceMode.MIXED:
            raise ValueError("case windows cannot use mixed mode")
        return value

    @field_validator("local_months")
    @classmethod
    def validate_months(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        ensure_unique_tuple(value, field_name="case local months")
        if any(type(month) is not int or not 1 <= month <= 12 for month in value):
            raise ValueError("local months must be integers from 1 through 12")
        return tuple(sorted(value))

    @field_validator("season_buckets")
    @classmethod
    def validate_seasons(
        cls,
        value: tuple[MeteorologicalSeason, ...],
    ) -> tuple[MeteorologicalSeason, ...]:
        ensure_unique_tuple(value, field_name="case season buckets")
        order = {season: index for index, season in enumerate(MeteorologicalSeason)}
        return tuple(sorted(value, key=order.__getitem__))

    @field_validator("parameter_keys")
    @classmethod
    def validate_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        ensure_unique_tuple(value, field_name="case window parameter keys")
        return tuple(sorted(value, key=lambda item: (item.code_system, item.code)))

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        duration = (self.time_range.end - self.time_range.start).total_seconds()
        if not duration.is_integer() or int(duration) != self.duration_seconds:
            raise ValueError("duration_seconds must match the UTC time range")
        return self

    def canonical_descriptor(self) -> CanonicalWindowDescriptorV1:
        duration_days = (
            Decimal(self.duration_seconds) / Decimal(86_400)
        ).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
        return CanonicalWindowDescriptorV1(
            window_kind=self.window_kind,
            duration_days=duration_days,
            local_months=self.local_months,
            season_buckets=self.season_buckets,
            resolution=self.resolution,
        )
