from pydantic import field_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.parameter_key import ParameterKey


class MonitoringStation(DomainModel):
    station_id: NonEmptyString
    source: NonEmptyString
    name: NonEmptyString | None
    location: GeoPoint | None
    associated_comid: NonEmptyString | None
    available_parameters: tuple[ParameterKey, ...] = ()

    @field_validator("available_parameters")
    @classmethod
    def validate_available_parameters(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        return ensure_unique_tuple(value, field_name="available_parameters")
