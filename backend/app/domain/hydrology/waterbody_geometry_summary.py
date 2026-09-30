from decimal import Decimal
from math import isfinite
from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import CanonicalDecimal, DomainModel
from app.domain.hydrology._geometry_types import (
    BoundingBox,
    Coordinate,
    DEFAULT_SIMPLIFICATION_TOLERANCE_M,
    MAX_SIMPLIFICATION_TOLERANCE_M,
    MAX_SIMPLIFIED_POINTS,
)


class WaterbodyGeometrySummary(DomainModel):
    geometry_type: Literal["linestring"] = "linestring"
    length_km: CanonicalDecimal
    bbox: BoundingBox
    simplified_coordinates: tuple[Coordinate, ...]
    simplification_tolerance_m: CanonicalDecimal

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        _validate_summary_coordinates(self.simplified_coordinates)
        _validate_bbox(self.bbox)
        if Decimal(self.length_km) <= 0:
            raise ValueError("length_km must be positive")
        tolerance = Decimal(self.simplification_tolerance_m)
        if not (
            DEFAULT_SIMPLIFICATION_TOLERANCE_M
            <= tolerance
            <= MAX_SIMPLIFICATION_TOLERANCE_M
        ):
            raise ValueError("simplification tolerance is outside supported bounds")
        if len(self.simplified_coordinates) > MAX_SIMPLIFIED_POINTS:
            raise ValueError("simplified geometry exceeds the point budget")
        return self


def _validate_summary_coordinates(
    coordinates: tuple[Coordinate, ...],
) -> None:
    if len(coordinates) < 2 or len(set(coordinates)) < 2:
        raise ValueError("simplified linestring requires two distinct coordinates")
    if any(
        not isfinite(longitude)
        or not isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
        for longitude, latitude in coordinates
    ):
        raise ValueError("simplified coordinates must be valid WGS84 lon/lat")


def _validate_bbox(bbox: BoundingBox) -> None:
    west, south, east, north = bbox
    if not all(isfinite(item) for item in bbox):
        raise ValueError("bbox values must be finite")
    if not -180 <= west <= 180 or not -180 <= east <= 180:
        raise ValueError("bbox longitude must be valid WGS84")
    if not -90 <= south <= north <= 90:
        raise ValueError("bbox latitude must be ordered WGS84")
