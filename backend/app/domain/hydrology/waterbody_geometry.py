from __future__ import annotations

from decimal import Decimal
from math import (
    acos,
    asin,
    atan2,
    cos,
    degrees,
    isfinite,
    radians,
    sin,
    sqrt,
)
from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainModel,
    NonEmptyString,
)
from app.domain.hydrology._geometry_types import (
    BoundingBox,
    Coordinate,
    DEFAULT_SIMPLIFICATION_TOLERANCE_M,
    EARTH_RADIUS_KM,
    MAX_SIMPLIFICATION_TOLERANCE_M,
    MAX_SIMPLIFIED_POINTS,
)
from app.domain.hydrology._topology_validation import validate_comid
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.waterbody_geometry_summary import (
    WaterbodyGeometrySummary,
)


class WaterbodyGeometry(DomainModel):
    geometry_ref: NonEmptyString
    geometry_type: Literal["linestring"] = "linestring"
    full_coordinates: tuple[Coordinate, ...]
    simplified_coordinates: tuple[Coordinate, ...]
    representative_point: GeoPoint
    bbox: BoundingBox
    length_km: CanonicalDecimal
    source: Literal["nldi"] = "nldi"
    source_comid: NonEmptyString
    simplification_tolerance_m: CanonicalDecimal

    @field_validator("source_comid")
    @classmethod
    def validate_source_comid(cls, value: str) -> str:
        return validate_comid(value)

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        expected = _derive_geometry(self.source_comid, self.full_coordinates)
        if self.geometry_ref != expected.geometry_ref:
            raise ValueError("geometry_ref must match source_comid")
        if self.simplified_coordinates != expected.simplified_coordinates:
            raise ValueError("simplified_coordinates must be derived from full geometry")
        if self.representative_point != expected.representative_point:
            raise ValueError("representative_point must be derived from full geometry")
        if self.bbox != expected.bbox:
            raise ValueError("bbox must be derived from full geometry")
        if self.length_km != expected.length_km:
            raise ValueError("length_km must be derived from full geometry")
        if self.simplification_tolerance_m != expected.tolerance_m:
            raise ValueError(
                "simplification_tolerance_m must be derived from full geometry"
            )
        return self

    @classmethod
    def from_coordinates(
        cls,
        *,
        source_comid: str,
        coordinates: tuple[tuple[float, float], ...],
    ) -> WaterbodyGeometry:
        normalized = tuple(
            (float(longitude), float(latitude))
            for longitude, latitude in coordinates
        )
        derived = _derive_geometry(source_comid, normalized)
        return cls(
            geometry_ref=derived.geometry_ref,
            full_coordinates=normalized,
            simplified_coordinates=derived.simplified_coordinates,
            representative_point=derived.representative_point,
            bbox=derived.bbox,
            length_km=derived.length_km,
            source_comid=source_comid,
            simplification_tolerance_m=derived.tolerance_m,
        )

    def summary(self) -> WaterbodyGeometrySummary:
        return WaterbodyGeometrySummary(
            length_km=self.length_km,
            bbox=self.bbox,
            simplified_coordinates=self.simplified_coordinates,
            simplification_tolerance_m=self.simplification_tolerance_m,
        )


class _DerivedGeometry(DomainModel):
    geometry_ref: NonEmptyString
    simplified_coordinates: tuple[Coordinate, ...]
    representative_point: GeoPoint
    bbox: BoundingBox
    length_km: CanonicalDecimal
    tolerance_m: CanonicalDecimal


def geodesic_distance_km(start: Coordinate, end: Coordinate) -> float:
    start_lon, start_lat = start
    end_lon, end_lat = end
    start_lat_radians = radians(start_lat)
    end_lat_radians = radians(end_lat)
    latitude_delta = end_lat_radians - start_lat_radians
    longitude_delta = _normalized_longitude_delta_radians(start_lon, end_lon)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(start_lat_radians)
        * cos(end_lat_radians)
        * sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, max(0.0, haversine))))


def linestring_length_km(coordinates: tuple[Coordinate, ...]) -> float:
    _validate_coordinates(coordinates)
    return sum(
        geodesic_distance_km(start, end)
        for start, end in zip(coordinates, coordinates[1:])
    )


def point_to_linestring_distance_km(
    point: GeoPoint,
    coordinates: tuple[Coordinate, ...],
) -> float:
    _validate_coordinates(coordinates)
    point_coordinate = (point.longitude, point.latitude)
    return min(
        _point_to_segment_distance_km(point_coordinate, start, end)
        for start, end in zip(coordinates, coordinates[1:])
    )


def sample_linestring(
    coordinates: tuple[Coordinate, ...],
    *,
    center_count: int,
) -> tuple[GeoPoint, ...]:
    _validate_coordinates(coordinates)
    if isinstance(center_count, bool) or center_count < 2:
        raise ValueError("center_count must be an integer of at least 2")
    segment_lengths = tuple(
        geodesic_distance_km(start, end)
        for start, end in zip(coordinates, coordinates[1:])
    )
    total_length = sum(segment_lengths)
    if total_length <= 0:
        raise ValueError("linestring must have positive length")
    return tuple(
        _point_at_distance(
            coordinates,
            segment_lengths,
            total_length * index / (center_count - 1),
        )
        for index in range(center_count)
    )


def _derive_geometry(
    source_comid: str,
    coordinates: tuple[Coordinate, ...],
) -> _DerivedGeometry:
    validate_comid(source_comid)
    _validate_coordinates(coordinates)
    length = sum(
        geodesic_distance_km(start, end)
        for start, end in zip(coordinates, coordinates[1:])
    )
    if not isfinite(length) or length <= 0:
        raise ValueError("linestring must have positive geodesic length")
    simplified, tolerance = _simplify_to_budget(coordinates)
    segment_lengths = tuple(
        geodesic_distance_km(start, end)
        for start, end in zip(coordinates, coordinates[1:])
    )
    latitudes = tuple(item[1] for item in coordinates)
    west, east = _longitude_bounds(tuple(item[0] for item in coordinates))
    canonical_length = _decimal_from_float(length)
    if canonical_length <= 0:
        raise ValueError("linestring length is below supported precision")
    return _DerivedGeometry(
        geometry_ref=f"geometry:{source_comid}",
        simplified_coordinates=simplified,
        representative_point=_point_at_distance(
            coordinates,
            segment_lengths,
            length / 2,
        ),
        bbox=(
            west,
            min(latitudes),
            east,
            max(latitudes),
        ),
        length_km=canonical_length,
        tolerance_m=Decimal(tolerance),
    )


def _validate_coordinates(coordinates: tuple[Coordinate, ...]) -> None:
    if len(coordinates) < 2:
        raise ValueError("linestring requires at least two coordinates")
    distinct: set[tuple[float, float]] = set()
    for longitude, latitude in coordinates:
        if not isfinite(longitude) or not isfinite(latitude):
            raise ValueError("linestring coordinates must be finite")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("linestring coordinates must be valid WGS84 lon/lat")
        distinct.add((longitude, latitude))
    if len(distinct) < 2:
        raise ValueError("linestring requires at least two distinct coordinates")


def _simplify_to_budget(
    coordinates: tuple[Coordinate, ...],
) -> tuple[tuple[Coordinate, ...], int]:
    simplified = _douglas_peucker(
        coordinates,
        DEFAULT_SIMPLIFICATION_TOLERANCE_M,
    )
    if len(simplified) <= MAX_SIMPLIFIED_POINTS:
        return simplified, DEFAULT_SIMPLIFICATION_TOLERANCE_M
    at_maximum = _douglas_peucker(
        coordinates,
        MAX_SIMPLIFICATION_TOLERANCE_M,
    )
    if len(at_maximum) > MAX_SIMPLIFIED_POINTS:
        raise ValueError(
            "linestring cannot satisfy the 256-point simplification budget "
            "within 250 meters"
        )
    lower = DEFAULT_SIMPLIFICATION_TOLERANCE_M + 1
    upper = MAX_SIMPLIFICATION_TOLERANCE_M
    best = at_maximum
    best_tolerance = upper
    while lower <= upper:
        candidate_tolerance = (lower + upper) // 2
        candidate = _douglas_peucker(coordinates, candidate_tolerance)
        if len(candidate) <= MAX_SIMPLIFIED_POINTS:
            best = candidate
            best_tolerance = candidate_tolerance
            upper = candidate_tolerance - 1
        else:
            lower = candidate_tolerance + 1
    return best, best_tolerance


def _douglas_peucker(
    coordinates: tuple[Coordinate, ...],
    tolerance_m: float,
) -> tuple[Coordinate, ...]:
    retained = {0, len(coordinates) - 1}
    pending = [(0, len(coordinates) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        maximum_distance_m = -1.0
        maximum_index: int | None = None
        for index in range(start_index + 1, end_index):
            distance_m = 1000 * _point_to_segment_distance_km(
                coordinates[index],
                coordinates[start_index],
                coordinates[end_index],
            )
            if distance_m > maximum_distance_m:
                maximum_distance_m = distance_m
                maximum_index = index
        if maximum_index is not None and maximum_distance_m > tolerance_m:
            retained.add(maximum_index)
            pending.append((maximum_index, end_index))
            pending.append((start_index, maximum_index))
    ordered = tuple(coordinates[index] for index in sorted(retained))
    if len(set(ordered)) >= 2:
        return ordered
    first = coordinates[0]
    replacement_index = max(
        range(1, len(coordinates) - 1),
        key=lambda index: geodesic_distance_km(first, coordinates[index]),
        default=0,
    )
    if replacement_index == 0:
        raise ValueError("linestring simplification lost all distinct points")
    return (coordinates[0], coordinates[replacement_index], coordinates[-1])


def _point_at_distance(
    coordinates: tuple[Coordinate, ...],
    segment_lengths: tuple[float, ...],
    target_distance_km: float,
) -> GeoPoint:
    if target_distance_km <= 0:
        longitude, latitude = coordinates[0]
        return GeoPoint(latitude=latitude, longitude=longitude)
    traversed = 0.0
    for index, segment_length in enumerate(segment_lengths):
        segment_end = traversed + segment_length
        if target_distance_km <= segment_end or index == len(segment_lengths) - 1:
            if segment_length == 0:
                traversed = segment_end
                continue
            fraction = min(
                1.0,
                max(0.0, (target_distance_km - traversed) / segment_length),
            )
            longitude, latitude = _interpolate_great_circle(
                coordinates[index],
                coordinates[index + 1],
                fraction,
            )
            return GeoPoint(
                latitude=_stable_float(latitude, decimal_places=12),
                longitude=_stable_float(longitude, decimal_places=12),
            )
        traversed = segment_end
    longitude, latitude = coordinates[-1]
    return GeoPoint(latitude=latitude, longitude=longitude)


def _interpolate_great_circle(
    start: Coordinate,
    end: Coordinate,
    fraction: float,
) -> tuple[float, float]:
    if fraction <= 0:
        return start
    if fraction >= 1:
        return end
    start_vector = _unit_vector(start)
    end_vector = _unit_vector(end)
    angle = _vector_angle(start_vector, end_vector)
    if angle < 1e-15:
        return start
    angle_sine = sin(angle)
    if abs(angle_sine) < 1e-15:
        longitude_delta = (
            (end[0] - start[0] + 540.0) % 360.0
        ) - 180.0
        longitude = _normalize_longitude(start[0] + fraction * longitude_delta)
        latitude = start[1] + fraction * (end[1] - start[1])
        return longitude, latitude
    start_weight = sin((1 - fraction) * angle) / angle_sine
    end_weight = sin(fraction * angle) / angle_sine
    vector = tuple(
        start_weight * start_value + end_weight * end_value
        for start_value, end_value in zip(start_vector, end_vector)
    )
    return _coordinate_from_vector(_normalize_vector(vector))


def _point_to_segment_distance_km(
    point: Coordinate,
    start: Coordinate,
    end: Coordinate,
) -> float:
    start_vector = _unit_vector(start)
    end_vector = _unit_vector(end)
    point_vector = _unit_vector(point)
    segment_angle = _vector_angle(start_vector, end_vector)
    if segment_angle < 1e-15:
        return EARTH_RADIUS_KM * _vector_angle(point_vector, start_vector)
    normal = _cross(start_vector, end_vector)
    normal_length = _norm(normal)
    if normal_length < 1e-15:
        return EARTH_RADIUS_KM * min(
            _vector_angle(point_vector, start_vector),
            _vector_angle(point_vector, end_vector),
        )
    unit_normal = tuple(item / normal_length for item in normal)
    projection = tuple(
        point_value - _dot(point_vector, unit_normal) * normal_value
        for point_value, normal_value in zip(point_vector, unit_normal)
    )
    projection_length = _norm(projection)
    candidates: list[tuple[float, float, float]] = []
    if projection_length >= 1e-15:
        projected = tuple(item / projection_length for item in projection)
        candidates.extend((projected, tuple(-item for item in projected)))
    distances = [
        _vector_angle(point_vector, start_vector),
        _vector_angle(point_vector, end_vector),
    ]
    for candidate in candidates:
        split_angle = _vector_angle(start_vector, candidate) + _vector_angle(
            candidate,
            end_vector,
        )
        if abs(split_angle - segment_angle) <= 1e-9:
            distances.append(_vector_angle(point_vector, candidate))
    return EARTH_RADIUS_KM * min(distances)


def _unit_vector(coordinate: Coordinate) -> tuple[float, float, float]:
    longitude, latitude = coordinate
    longitude_radians = radians(longitude)
    latitude_radians = radians(latitude)
    latitude_cosine = cos(latitude_radians)
    return (
        latitude_cosine * cos(longitude_radians),
        latitude_cosine * sin(longitude_radians),
        sin(latitude_radians),
    )


def _coordinate_from_vector(
    vector: tuple[float, float, float],
) -> tuple[float, float]:
    x_value, y_value, z_value = vector
    longitude = _normalize_longitude(degrees(atan2(y_value, x_value)))
    latitude = degrees(atan2(z_value, sqrt(x_value**2 + y_value**2)))
    return longitude, latitude


def _vector_angle(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return acos(min(1.0, max(-1.0, _dot(left, right))))


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return sum(left_value * right_value for left_value, right_value in zip(left, right))


def _norm(vector: tuple[float, float, float]) -> float:
    return sqrt(_dot(vector, vector))


def _normalize_vector(
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    length = _norm(vector)
    if length == 0:
        raise ValueError("cannot normalize zero-length vector")
    return tuple(item / length for item in vector)


def _normalized_longitude_delta_radians(start: float, end: float) -> float:
    return radians(((end - start + 540.0) % 360.0) - 180.0)


def _normalize_longitude(longitude: float) -> float:
    normalized = ((longitude + 180.0) % 360.0) - 180.0
    return 180.0 if normalized == -180.0 and longitude > 0 else normalized


def _longitude_bounds(longitudes: tuple[float, ...]) -> tuple[float, float]:
    originals_by_shifted: dict[float, float] = {}
    for longitude in longitudes:
        originals_by_shifted.setdefault(longitude % 360.0, longitude)
    shifted = sorted(originals_by_shifted)
    if len(shifted) == 1:
        longitude = originals_by_shifted[shifted[0]]
        return longitude, longitude
    gaps = tuple(
        (
            (shifted[(index + 1) % len(shifted)] - shifted[index]) % 360.0,
            index,
        )
        for index in range(len(shifted))
    )
    _, gap_start_index = max(gaps, key=lambda item: (item[0], -item[1]))
    west = shifted[(gap_start_index + 1) % len(shifted)]
    east = shifted[gap_start_index]
    return originals_by_shifted[west], originals_by_shifted[east]


def _decimal_from_float(value: float) -> Decimal:
    return Decimal(f"{value:.9f}")


def _stable_float(value: float, *, decimal_places: int) -> float:
    return float(f"{value:.{decimal_places}f}")
