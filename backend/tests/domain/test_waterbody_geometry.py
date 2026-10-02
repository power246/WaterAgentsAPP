from math import isclose

import pytest
from pydantic import ValidationError

from app.domain import (
    GeoPoint,
    WaterbodyGeometry,
    point_to_linestring_distance_km,
)


def test_geometry_derives_stable_geodesic_fields_for_curved_line() -> None:
    coordinates = (
        (-77.24, 38.89),
        (-77.18, 38.94),
        (-77.13, 38.95),
        (-77.02, 39.01),
    )

    first = WaterbodyGeometry.from_coordinates(
        source_comid="12345678",
        coordinates=coordinates,
    )
    second = WaterbodyGeometry.from_coordinates(
        source_comid="12345678",
        coordinates=coordinates,
    )

    assert first == second
    assert first.geometry_ref == "geometry:12345678"
    assert first.bbox == (-77.24, 38.89, -77.02, 39.01)
    assert float(first.length_km) > 20
    assert first.simplification_tolerance_m == "25"
    assert len(first.simplified_coordinates) <= len(coordinates)


def test_dateline_and_high_latitude_geometry_use_short_geodesic_path() -> None:
    dateline = WaterbodyGeometry.from_coordinates(
        source_comid="1",
        coordinates=((179.9, 70.0), (-179.9, 70.0)),
    )
    high_latitude = WaterbodyGeometry.from_coordinates(
        source_comid="2",
        coordinates=((-45.0, 89.0), (45.0, 89.0)),
    )

    assert float(dateline.length_km) < 10
    assert abs(dateline.representative_point.longitude) > 179.9
    assert dateline.bbox[0] > dateline.bbox[2]
    assert 150 < float(high_latitude.length_km) < 160


def test_repeated_points_are_retained_but_all_repeated_line_is_rejected() -> None:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="3",
        coordinates=((-77.0, 39.0), (-77.0, 39.0), (-76.9, 39.0)),
    )

    assert geometry.full_coordinates[0] == geometry.full_coordinates[1]
    with pytest.raises(ValueError, match="distinct"):
        WaterbodyGeometry.from_coordinates(
            source_comid="3",
            coordinates=((-77.0, 39.0), (-77.0, 39.0)),
        )


@pytest.mark.parametrize(
    "coordinates",
    (
        (),
        ((-77.0, 39.0),),
        ((-181.0, 39.0), (-77.0, 39.0)),
        ((-77.0, 91.0), (-76.0, 39.0)),
        ((float("inf"), 39.0), (-76.0, 39.0)),
    ),
)
def test_geometry_rejects_empty_short_nonfinite_and_invalid_wgs84_lines(
    coordinates,
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        WaterbodyGeometry.from_coordinates(
            source_comid="4",
            coordinates=coordinates,
        )


def test_simplification_increases_tolerance_to_meet_bounded_summary() -> None:
    coordinates = tuple(
        (
            -77.0 + index * 0.0001,
            39.0 + (0.0005 if index % 2 else 0.0),
        )
        for index in range(600)
    )

    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="5",
        coordinates=coordinates,
    )

    assert len(geometry.simplified_coordinates) <= 256
    assert 25 <= int(geometry.simplification_tolerance_m) <= 250


def test_simplification_fails_when_250_meters_cannot_meet_point_budget() -> None:
    coordinates = tuple(
        (
            -77.0 + index * 0.0001,
            39.0 + (0.01 if index % 2 else 0.0),
        )
        for index in range(300)
    )

    with pytest.raises(ValueError, match="256-point.*250 meters"):
        WaterbodyGeometry.from_coordinates(
            source_comid="5",
            coordinates=coordinates,
        )


def test_exact_distance_uses_full_line_not_simplified_summary() -> None:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="6",
        coordinates=((-77.0, 39.0), (-76.9, 39.1), (-76.8, 39.0)),
    )
    point = GeoPoint(latitude=39.1, longitude=-76.9)

    distance = point_to_linestring_distance_km(
        point,
        geometry.full_coordinates,
    )

    assert isclose(distance, 0.0, abs_tol=1e-6)
