from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.time.time_range import TimeRange
from app.domain.time.timezone import get_iana_timezone


class WaterObservationRequest(DomainModel):
    operation_id: NonEmptyString
    provider: ParameterProvider
    channel: NonEmptyString
    station_id: NonEmptyString
    series_refs: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    request_range: TimeRange
    analysis_timezone: NonEmptyString
    affected_window_ids: tuple[NonEmptyString, ...]

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        for field_name in ("series_refs", "parameter_keys", "affected_window_ids"):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        return self
