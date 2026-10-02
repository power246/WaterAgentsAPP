from typing import TypeAlias

from app.domain.common import DomainFloat

EARTH_RADIUS_KM = 6371.0088
DEFAULT_SIMPLIFICATION_TOLERANCE_M = 25
MAX_SIMPLIFICATION_TOLERANCE_M = 250
MAX_SIMPLIFIED_POINTS = 256

Coordinate: TypeAlias = tuple[DomainFloat, DomainFloat]
BoundingBox: TypeAlias = tuple[
    DomainFloat,
    DomainFloat,
    DomainFloat,
    DomainFloat,
]
