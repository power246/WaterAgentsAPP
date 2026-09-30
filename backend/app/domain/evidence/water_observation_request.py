from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.time.time_range import TimeRange


class WaterObservationRequest(DomainModel):
    operation_id: NonEmptyString
    provider: ParameterProvider
    channel: NonEmptyString
    station_id: NonEmptyString
    series_refs: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    request_range: TimeRange
    affected_window_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        for field_name in ("series_refs", "parameter_keys", "affected_window_ids"):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        return self
