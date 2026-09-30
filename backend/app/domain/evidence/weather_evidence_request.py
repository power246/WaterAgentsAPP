from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.location import GeoPoint
from app.domain.time.time_range import TimeRange


class WeatherEvidenceRequest(DomainModel):
    operation_id: NonEmptyString
    evidence_kind: Literal["observation", "forecast"]
    location: GeoPoint
    variables: tuple[NonEmptyString, ...]
    request_range: TimeRange
    affected_window_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        for field_name in ("variables", "affected_window_ids"):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        return self
