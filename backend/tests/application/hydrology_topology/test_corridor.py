from datetime import datetime, timezone

import pytest

from app.application.hydrology_topology import (
    FacilityRadiusTooSmallForBudgetError,
    WaterbodyGeometryUnavailableError,
    build_facility_corridor_plan,
    describe_facility_corridor,
    filter_facilities_to_corridor,
)
from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    ComidLayer,
    CoverageUnit,
    EvidenceLocation,
    FacilityRecord,
    GeoPoint,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    ParameterKey,
    ParameterProvider,
    Provenance,
    ProviderQuality,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologyStationRef,
    TopologySeriesRef,
    WaterbodyGeometry,
    geodesic_distance_km,
    point_to_linestring_distance_km,
)

UNIT = CoverageUnit(
    parameter=ParameterKey(code_system="usgs", code="00060"),
    segment_index=0,
)
NOW = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)


def graph_for_geometry(
    geometry: WaterbodyGeometry | None,
) -> HydrologyGraph:
    point = (
        geometry.representative_point
        if geometry is not None
        else GeoPoint(latitude=39.0, longitude=-77.0)
    )
    node = HydrologyNode(
        comid="123",
        point=point,
        geometry_ref=geometry.geometry_ref if geometry is not None else None,
        geometry_summary=geometry.summary() if geometry is not None else None,
        depth=0,
        cumulative_distance_km=0.0,
        is_target=True,
        is_inlet=True,
        participation=HydrologyParticipation.ACTIVE,
        qualified_units=(UNIT,),
        station_refs=(
            TopologyStationRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id="USGS-123",
                series_refs=(
                    TopologySeriesRef(
                        series_identity="series-123",
                        parameter=UNIT.parameter,
                        channel="continuous",
                        covered_segment_indexes=(0,),
                    ),
                ),
            ),
        ),
    )
    return HydrologyGraph(
        target_comid="123",
        coverage=TopologyCoverage.COMPLETE,
        nodes=(node,),
        geometries=(
            {geometry.geometry_ref: geometry} if geometry is not None else {}
        ),
        edges=(),
        branch_outcomes=(
            BranchOutcome(
                branch_root_comid="123",
                coverage_unit=UNIT,
                outcome=BranchOutcomeStatus.QUALIFIED,
                stop_comid="123",
                path_to_target=("123",),
                reason=BranchOutcomeReason.TARGET_STATION_QUALIFIED,
            ),
        ),
        execution_layers=(ComidLayer(("123",)),),
        limits=TopologyLimits(
            max_distance_km=100.0,
            max_hops=12,
            max_nodes=128,
        ),
        stats=TopologySearchStats(
            discovered_nodes=1,
            queried_nodes=1,
            max_depth=0,
            max_distance_km=0.0,
            qualified_inlets=1,
            excluded_nodes=0,
        ),
    )


def equator_geometry(longitude_span: float) -> WaterbodyGeometry:
    return WaterbodyGeometry.from_coordinates(
        source_comid="123",
        coordinates=((0.0, 0.0), (longitude_span, 0.0)),
    )


def facility(
    facility_id: str,
    *,
    latitude: float,
    longitude: float,
    permit_id: str | None = "permit-1",
    provider_record_id: str | None = None,
) -> FacilityRecord:
    return FacilityRecord(
        provider="epa_echo",
        channel="cwa_facility",
        source_uri="https://example.test/facility",
        retrieved_at=NOW,
        observed_time=NOW,
        location=EvidenceLocation(
            point=GeoPoint(latitude=latitude, longitude=longitude),
            comid=None,
            station_id=None,
            distance_to_target_km=None,
        ),
        provider_quality=ProviderQuality(approval="official"),
        provenance=Provenance(
            provider_record_id=provider_record_id or facility_id
        ),
        registry_id="echo",
        facility_id=facility_id,
        permit_id=permit_id,
        program="NPDES",
        status="active",
    )


def test_descriptor_exposes_bounded_summary_and_request_estimates() -> None:
    geometry = equator_geometry(1.0)

    descriptor = describe_facility_corridor(graph_for_geometry(geometry))

    assert descriptor.geometry_ref == geometry.geometry_ref
    assert descriptor.simplified_coordinates == geometry.simplified_coordinates
    assert [
        item.provider_radius_miles
        for item in descriptor.candidate_radius_estimates
    ] == [1, 2, 3, 4, 5, 10, 20]
    assert descriptor.candidate_radius_estimates[0].estimated_provider_requests > 32
    assert not hasattr(descriptor, "full_coordinates")


@pytest.mark.parametrize("radius", (0.99, 20.01, float("nan"), True))
def test_agent_radius_outside_one_to_twenty_miles_is_rejected(radius) -> None:
    with pytest.raises(ValueError, match="radius"):
        build_facility_corridor_plan(
            graph_for_geometry(equator_geometry(0.1)),
            agent_radius_miles=radius,
        )


def test_provider_radius_rounds_up_and_samples_complete_full_corridor() -> None:
    geometry = equator_geometry(0.5)

    plan = build_facility_corridor_plan(
        graph_for_geometry(geometry),
        agent_radius_miles="3.1",
    )

    assert plan.agent_radius_miles == "3.1"
    assert plan.provider_radius_miles == 4
    assert plan.centers[0] == GeoPoint(latitude=0.0, longitude=0.0)
    assert plan.centers[-1] == GeoPoint(latitude=0.0, longitude=0.5)
    gaps_miles = [
        geodesic_distance_km(
            (start.longitude, start.latitude),
            (end.longitude, end.latitude),
        )
        / 1.609344
        for start, end in zip(plan.centers, plan.centers[1:])
    ]
    assert max(gaps_miles) <= 4 + 1e-9


def test_center_budget_boundary_and_minimum_feasible_radius_are_exact() -> None:
    boundary_graph = graph_for_geometry(equator_geometry(0.44))
    boundary = build_facility_corridor_plan(
        boundary_graph,
        agent_radius_miles=1,
    )
    assert len(boundary.centers) == 32

    graph = graph_for_geometry(equator_geometry(1.0))
    with pytest.raises(FacilityRadiusTooSmallForBudgetError) as captured:
        build_facility_corridor_plan(graph, agent_radius_miles=1)
    assert captured.value.estimated_provider_requests > 32
    assert captured.value.minimum_feasible_radius_miles == "3"

    feasible = build_facility_corridor_plan(graph, agent_radius_miles=3)
    assert len(feasible.centers) <= 32
    assert feasible.centers[0].longitude == 0.0
    assert feasible.centers[-1].longitude == 1.0

    maximum = build_facility_corridor_plan(
        graph_for_geometry(equator_geometry(5.0)),
        agent_radius_miles=20,
    )
    assert maximum.provider_radius_miles == 20
    assert len(maximum.centers) <= 32

    with pytest.raises(FacilityRadiusTooSmallForBudgetError) as constrained:
        build_facility_corridor_plan(
            boundary_graph,
            agent_radius_miles=1,
            center_budget=10,
        )
    assert constrained.value.estimated_provider_requests == 32
    constrained_plan = build_facility_corridor_plan(
        boundary_graph,
        agent_radius_miles=constrained.value.minimum_feasible_radius_miles,
        center_budget=10,
    )
    assert len(constrained_plan.centers) <= 10


def test_geometry_unavailable_has_stable_error_without_point_fallback() -> None:
    with pytest.raises(WaterbodyGeometryUnavailableError) as captured:
        build_facility_corridor_plan(
            graph_for_geometry(None),
            agent_radius_miles=20,
        )

    assert captured.value.code == "waterbody_geometry_unavailable"


def test_final_filter_deduplicates_and_uses_exact_full_line_distance() -> None:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="123",
        coordinates=((-0.01, 0.0), (0.0, 0.0002), (0.01, 0.0)),
    )
    assert geometry.simplified_coordinates == ((-0.01, 0.0), (0.01, 0.0))
    on_full_line_side = facility(
        "inside",
        latitude=0.0146,
        longitude=0.0,
        provider_record_id="inside-b",
    )
    duplicate = facility(
        "inside",
        latitude=0.0146,
        longitude=0.0,
        provider_record_id="inside-a",
    )
    outside = facility("outside", latitude=0.02, longitude=0.0)
    missing_coordinates = facility("missing", latitude=0.0, longitude=0.0)
    missing_coordinates = missing_coordinates.model_copy(
        update={
            "location": EvidenceLocation(
                point=None,
                comid="123",
                station_id=None,
                distance_to_target_km=None,
            )
        }
    )

    filtered = filter_facilities_to_corridor(
        (on_full_line_side, outside, duplicate, missing_coordinates),
        geometry=geometry,
        agent_radius_miles=1,
    )

    assert [item.facility_id for item in filtered] == ["inside"]
    assert filtered[0].record_id == "inside-a"
    assert filtered[0].location.distance_to_target_km is not None
    assert filtered[0].location.distance_to_target_km < 1.609344
    simplified_distance = point_to_linestring_distance_km(
        filtered[0].location.point,
        geometry.simplified_coordinates,
    )
    assert simplified_distance > 1.609344
