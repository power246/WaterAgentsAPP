from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import CanonicalDecimal, NonEmptyString
from app.domain.evidence.source_record_base import _SourceRecordBase
from app.domain.hydrology.parameter_key import ParameterKey


class DischargeObservationRecord(_SourceRecordBase):
    record_type: Literal["discharge_observation"] = "discharge_observation"
    parameter: ParameterKey
    value: CanonicalDecimal
    medium: NonEmptyString
    fraction: NonEmptyString | None = None
    method: NonEmptyString | None = None
    statistic: NonEmptyString

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observed_time is None:
            raise ValueError("discharge observation requires observed_time")
        if self.location.station_id is None:
            raise ValueError("discharge observation requires station_id")
        if self.raw_unit is None:
            raise ValueError("discharge observation requires raw_unit")
        return self
