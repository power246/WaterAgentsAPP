from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Self

from pydantic import Field, model_validator

from app.domain import (
    BoundingBox,
    CanonicalDecimal,
    Coordinate,
    DomainModel,
    FacilityRecord,
    GeoPoint,
    HydrologyGraph,
    NonEmptyString,
    WaterbodyGeometry,
    point_to_linestring_distance_km,
    sample_linestring,
)

FACILITY_RADIUS_MIN_MILES = Decimal("1")
FACILITY_RADIUS_MAX_MILES = Decimal("20")
FACILITY_PROVIDER_RADII_MILES = (1, 2, 3, 4, 5, 10, 20)
FACILITY_CENTER_BUDGET = 32
KM_PER_MILE = Decimal("1.609344")


class WaterbodyGeometryUnavailableError(ValueError):
    code = "waterbody_geometry_unavailable"


class FacilityRadiusTooSmallForBudgetError(ValueError):
    code = "facility_radius_too_small_for_budget"

    def __init__(
        self,
        *,
        requested_radius_miles: str,
        estimated_provider_requests: int,
        minimum_feasible_radius_miles: str | None,
    ) -> None:
        self.requested_radius_miles = requested_radius_miles
        self.estimated_provider_requests = estimated_provider_requests
        self.minimum_feasible_radius_miles = minimum_feasible_radius_miles
        minimum = minimum_feasible_radius_miles or "unavailable within 20 miles"
        super().__init__(
            "facility radius requires too many corridor centers; "
            f"minimum feasible radius is {minimum}"
        )


class FacilityRadiusEstimate(DomainModel):
    candidate_radius_miles: CanonicalDecimal
    provider_radius_miles: int = Field(strict=True, ge=1, le=20)
    estimated_provider_requests: int = Field(strict=True, ge=2)

    @model_validator(mode="after")
    def validate_supported_radius(self) -> Self:
        if self.provider_radius_miles not in FACILITY_PROVIDER_RADII_MILES:
            raise ValueError("provider radius is not supported")
        if Decimal(self.candidate_radius_miles) != self.provider_radius_miles:
            raise ValueError("candidate radius must be a supported provider radius")
        return self


class FacilityCorridorDescriptor(DomainModel):
    geometry_ref: NonEmptyString
    length_km: CanonicalDecimal
    bbox: BoundingBox
    simplified_coordinates: tuple[Coordinate, ...]
    minimum_radius_miles: CanonicalDecimal = "1"
    maximum_radius_miles: CanonicalDecimal = "20"
    candidate_radius_estimates: tuple[FacilityRadiusEstimate, ...]
    center_budget: int = Field(default=FACILITY_CENTER_BUDGET, strict=True)

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        if Decimal(self.minimum_radius_miles) != FACILITY_RADIUS_MIN_MILES:
            raise ValueError("minimum facility radius must be 1 mile")
        if Decimal(self.maximum_radius_miles) != FACILITY_RADIUS_MAX_MILES:
            raise ValueError("maximum facility radius must be 20 miles")
        if tuple(
            item.provider_radius_miles for item in self.candidate_radius_estimates
        ) != FACILITY_PROVIDER_RADII_MILES:
            raise ValueError("radius estimates must cover supported provider radii")
        return self


class FacilityCorridorPlan(DomainModel):
    geometry_ref: NonEmptyString
    agent_radius_miles: CanonicalDecimal
    provider_radius_miles: int = Field(strict=True, ge=1, le=20)
    centers: tuple[GeoPoint, ...]

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if not (
            FACILITY_RADIUS_MIN_MILES
            <= Decimal(self.agent_radius_miles)
            <= FACILITY_RADIUS_MAX_MILES
        ):
            raise ValueError("agent facility radius must be within 1 and 20 miles")
        expected_provider_radius = _provider_radius(
            Decimal(self.agent_radius_miles)
        )
        if self.provider_radius_miles != expected_provider_radius:
            raise ValueError("provider radius must round the Agent radius upward")
        if not 2 <= len(self.centers) <= FACILITY_CENTER_BUDGET:
            raise ValueError("corridor plan center count is outside supported bounds")
        return self


def describe_facility_corridor(
    graph: HydrologyGraph,
) -> FacilityCorridorDescriptor:
    geometry = _target_geometry(graph)
    return FacilityCorridorDescriptor(
        geometry_ref=geometry.geometry_ref,
        length_km=geometry.length_km,
        bbox=geometry.bbox,
        simplified_coordinates=geometry.simplified_coordinates,
        candidate_radius_estimates=tuple(
            FacilityRadiusEstimate(
                candidate_radius_miles=radius,
                provider_radius_miles=radius,
                estimated_provider_requests=_estimated_center_count(
                    geometry,
                    radius,
                ),
            )
            for radius in FACILITY_PROVIDER_RADII_MILES
        ),
    )


def build_facility_corridor_plan(
    graph: HydrologyGraph,
    *,
    agent_radius_miles: str | int | float | Decimal,
) -> FacilityCorridorPlan:
    geometry = _target_geometry(graph)
    exact_radius = _validate_agent_radius(agent_radius_miles)
    provider_radius = _provider_radius(exact_radius)
    center_count = _estimated_center_count(geometry, provider_radius)
    if center_count > FACILITY_CENTER_BUDGET:
        feasible = next(
            (
                candidate
                for candidate in FACILITY_PROVIDER_RADII_MILES
                if _estimated_center_count(geometry, candidate)
                <= FACILITY_CENTER_BUDGET
            ),
            None,
        )
        raise FacilityRadiusTooSmallForBudgetError(
            requested_radius_miles=_canonical_decimal(exact_radius),
            estimated_provider_requests=center_count,
            minimum_feasible_radius_miles=(
                str(feasible) if feasible is not None else None
            ),
        )
    centers = sample_linestring(
        geometry.full_coordinates,
        center_count=center_count,
    )
    return FacilityCorridorPlan(
        geometry_ref=geometry.geometry_ref,
        agent_radius_miles=exact_radius,
        provider_radius_miles=provider_radius,
        centers=centers,
    )


def filter_facilities_to_corridor(
    records: tuple[FacilityRecord, ...],
    *,
    geometry: WaterbodyGeometry,
    agent_radius_miles: str | int | float | Decimal,
) -> tuple[FacilityRecord, ...]:
    exact_radius = _validate_agent_radius(agent_radius_miles)
    maximum_distance_km = float(exact_radius * KM_PER_MILE)
    deduplicated: dict[tuple[str, str, str], FacilityRecord] = {}
    for record in sorted(
        records,
        key=lambda item: (*_facility_identity(item), item.record_id),
    ):
        identity = _facility_identity(record)
        if identity not in deduplicated:
            deduplicated[identity] = record

    selected: list[FacilityRecord] = []
    for record in deduplicated.values():
        point = record.location.point
        if point is None:
            continue
        distance_km = point_to_linestring_distance_km(
            point,
            geometry.full_coordinates,
        )
        if distance_km > maximum_distance_km + 1e-9:
            continue
        payload = record.model_dump(mode="python")
        payload["location"] = record.location.model_copy(
            update={"distance_to_target_km": float(f"{distance_km:.9f}")}
        ).model_dump(mode="python")
        if record.provenance.provider_record_id is None:
            payload["record_id"] = None
        selected.append(FacilityRecord.model_validate(payload))
    return tuple(
        sorted(selected, key=lambda item: (*_facility_identity(item), item.record_id))
    )


def _target_geometry(graph: HydrologyGraph) -> WaterbodyGeometry:
    target = next(node for node in graph.nodes if node.is_target)
    if target.geometry_ref is None:
        raise WaterbodyGeometryUnavailableError(
            "target waterbody geometry is unavailable"
        )
    geometry = graph.geometries.get(target.geometry_ref)
    if geometry is None:
        raise WaterbodyGeometryUnavailableError(
            "target waterbody geometry is unavailable"
        )
    return geometry


def _validate_agent_radius(value: str | int | float | Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("agent facility radius must be a finite number")
    try:
        radius = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("agent facility radius must be a finite number") from error
    if not radius.is_finite():
        raise ValueError("agent facility radius must be a finite number")
    if not FACILITY_RADIUS_MIN_MILES <= radius <= FACILITY_RADIUS_MAX_MILES:
        raise ValueError("agent facility radius must be within 1 and 20 miles")
    return radius


def _provider_radius(agent_radius: Decimal) -> int:
    return next(
        radius
        for radius in FACILITY_PROVIDER_RADII_MILES
        if agent_radius <= radius
    )


def _estimated_center_count(
    geometry: WaterbodyGeometry,
    provider_radius_miles: int,
) -> int:
    length_miles = Decimal(geometry.length_km) / KM_PER_MILE
    interval_count = int(
        (length_miles / Decimal(provider_radius_miles)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(2, interval_count + 1)


def _facility_identity(record: FacilityRecord) -> tuple[str, str, str]:
    return record.registry_id, record.facility_id, record.permit_id or ""


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
