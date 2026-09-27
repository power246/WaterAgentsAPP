from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_provider import ParameterProvider


class TopologyStationRef(DomainModel):
    provider: ParameterProvider
    station_id: NonEmptyString
    series_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_series_refs(self) -> Self:
        if not self.series_refs:
            raise ValueError("series_refs must not be empty")
        ensure_unique_tuple(self.series_refs, field_name="series_refs")
        if self.series_refs != tuple(sorted(self.series_refs)):
            raise ValueError("series_refs must be sorted")
        return self
