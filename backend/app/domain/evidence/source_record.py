from typing import Annotated, TypeAlias

from pydantic import Field

from app.domain.evidence.discharge_observation_record import (
    DischargeObservationRecord,
)
from app.domain.evidence.facility_record import FacilityRecord
from app.domain.evidence.historical_transcript_record import HistoricalTranscriptRecord
from app.domain.evidence.upstream_result_record import UpstreamResultRecord
from app.domain.evidence.water_observation_record import WaterObservationRecord
from app.domain.evidence.weather_forecast_record import WeatherForecastRecord
from app.domain.evidence.weather_observation_record import WeatherObservationRecord

SourceRecord: TypeAlias = Annotated[
    WaterObservationRecord
    | DischargeObservationRecord
    | WeatherObservationRecord
    | WeatherForecastRecord
    | FacilityRecord
    | UpstreamResultRecord
    | HistoricalTranscriptRecord,
    Field(discriminator="record_type"),
]
