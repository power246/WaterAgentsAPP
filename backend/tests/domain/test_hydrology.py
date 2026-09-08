import math

import pytest
from pydantic import ValidationError

from app.domain.hydrology import (
    GeoPoint,
    MonitoringStation,
    ParameterKey,
    RiverSegment,
    WaterQualityParameter,
)


def assert_json_round_trip(model) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_geo_point_accepts_boundaries_zero_and_round_trips() -> None:
    for point in (
        GeoPoint(latitude=-90, longitude=-180),
        GeoPoint(latitude=0, longitude=0),
        GeoPoint(latitude=90, longitude=180),
    ):
        assert_json_round_trip(point)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (-90.1, 0),
        (90.1, 0),
        (0, -180.1),
        (0, 180.1),
        (math.nan, 0),
        (0, math.inf),
        (True, 0),
        (0, False),
    ],
)
def test_geo_point_rejects_invalid_coordinates(
    latitude: float,
    longitude: float,
) -> None:
    with pytest.raises(ValidationError):
        GeoPoint(latitude=latitude, longitude=longitude)


def test_geo_point_converts_integer_coordinates_to_float() -> None:
    point = GeoPoint(latitude=1, longitude=0)

    assert isinstance(point.latitude, float)
    assert isinstance(point.longitude, float)


def test_river_segment_accepts_missing_location_and_round_trips() -> None:
    segment = RiverSegment(
        comid="001234",
        location=None,
        upstream_comids=("000100", "000200"),
        downstream_comids=("009999",),
    )

    assert segment.location is None
    assert_json_round_trip(segment)


@pytest.mark.parametrize(
    ("upstream", "downstream", "message"),
    [
        (("2", "2"), (), "upstream_comids must not contain duplicates"),
        ((), ("2", "2"), "downstream_comids must not contain duplicates"),
        (("1",), (), "must not reference itself as upstream"),
        ((), ("1",), "must not reference itself as downstream"),
    ],
)
def test_river_segment_rejects_invalid_relationships(
    upstream: tuple[str, ...],
    downstream: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        RiverSegment(
            comid="1",
            location=None,
            upstream_comids=upstream,
            downstream_comids=downstream,
        )


def test_parameter_key_preserves_identity_and_leading_zeroes() -> None:
    key = ParameterKey(code_system="usgs", code="00400")

    assert key != ParameterKey(code_system="epa", code="00400")
    assert_json_round_trip(key)
    assert ParameterKey.model_validate_json(key.model_dump_json()).code == "00400"


def test_water_quality_parameter_validates_units_and_round_trips() -> None:
    ph = WaterQualityParameter(
        key=ParameterKey(code_system="usgs", code="00400"),
        name="pH",
        canonical_unit=None,
        is_dimensionless=True,
        category="physical",
    )
    temperature = WaterQualityParameter(
        key=ParameterKey(code_system="usgs", code="00010"),
        name="Temperature",
        canonical_unit="degC",
        category="physical",
    )

    assert_json_round_trip(ph)
    assert_json_round_trip(temperature)


def test_dimensional_parameter_requires_canonical_unit() -> None:
    with pytest.raises(ValidationError, match="canonical_unit is required"):
        WaterQualityParameter(
            key=ParameterKey(code_system="usgs", code="00010"),
            name="Temperature",
            canonical_unit=None,
            category=None,
        )


def test_monitoring_station_accepts_missing_location_and_round_trips() -> None:
    station = MonitoringStation(
        station_id="04024000",
        source="usgs",
        name=None,
        location=None,
        associated_comid="001234",
        available_parameters=(
            ParameterKey(code_system="usgs", code="00400"),
            ParameterKey(code_system="epa", code="00400"),
        ),
    )

    assert station.location is None
    assert_json_round_trip(station)


def test_monitoring_station_rejects_duplicate_parameter_keys() -> None:
    key = ParameterKey(code_system="usgs", code="00400")

    with pytest.raises(ValidationError, match="must not contain duplicates"):
        MonitoringStation(
            station_id="04024000",
            source="usgs",
            name="Station",
            location=GeoPoint(latitude=0, longitude=0),
            associated_comid=None,
            available_parameters=(key, key),
        )


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (RiverSegment, {"comid": " ", "location": None}),
        (ParameterKey, {"code_system": " ", "code": "00400"}),
        (ParameterKey, {"code_system": "usgs", "code": " "}),
        (
            WaterQualityParameter,
            {
                "key": {"code_system": "usgs", "code": "00400"},
                "name": " ",
                "canonical_unit": None,
                "is_dimensionless": True,
                "category": None,
            },
        ),
        (
            MonitoringStation,
            {
                "station_id": " ",
                "source": "usgs",
                "name": None,
                "location": None,
                "associated_comid": None,
            },
        ),
    ],
)
def test_hydrology_models_reject_empty_required_strings(model, values) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)
