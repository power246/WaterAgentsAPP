from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain import (
    AvailabilityCoverageSource,
    CoverageUnit,
    GeoPoint,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    SeriesRef,
    StationAvailability,
)

UTC = timezone.utc
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
UNIT = CoverageUnit(parameter=DISCHARGE, segment_index=0)
STATION = MonitoringStation(
    station_id="USGS-A",
    source="nwissite",
    name="Station A",
    location=GeoPoint(latitude=38.9, longitude=-77.0),
    associated_comid="12345678",
)
REFERENCE = SeriesRef(
    provider=ParameterProvider.WDFN_TIME_SERIES,
    station_id="USGS-A",
    parameter=DISCHARGE,
    series_identity="wdfn:v1:series-a",
    coverage_source=(
        AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_ENVELOPE
    ),
    covered_units=(UNIT,),
)


def test_station_availability_requires_hydrologic_association_and_exact_union() -> None:
    valid = StationAvailability(
        station=STATION,
        comid="12345678",
        providers=(ParameterProvider.WDFN_TIME_SERIES,),
        covered_units=(UNIT,),
        series_refs=(REFERENCE,),
        checked_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    assert valid.covered_units == (UNIT,)

    with pytest.raises(ValidationError, match="hydrologic association"):
        StationAvailability(
            station=STATION,
            comid="99999999",
            providers=(ParameterProvider.WDFN_TIME_SERIES,),
            covered_units=(UNIT,),
            series_refs=(REFERENCE,),
            checked_at=datetime(2026, 9, 27, tzinfo=UTC),
        )

    with pytest.raises(ValidationError, match="series-ref union"):
        StationAvailability(
            station=STATION,
            comid="12345678",
            providers=(ParameterProvider.WDFN_TIME_SERIES,),
            covered_units=(
                UNIT,
                CoverageUnit(parameter=DISCHARGE, segment_index=1),
            ),
            series_refs=(REFERENCE,),
            checked_at=datetime(2026, 9, 27, tzinfo=UTC),
        )
