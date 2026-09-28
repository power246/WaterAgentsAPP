import pytest
from pydantic import ValidationError

from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    ComidLayer,
    CoverageUnit,
    GeoPoint,
    HydrologyEdge,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    ParameterKey,
    ParameterProvider,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologyStationRef,
)

UNIT = CoverageUnit(
    parameter=ParameterKey(code_system="usgs", code="00060"),
    segment_index=0,
)


def target_node() -> HydrologyNode:
    return HydrologyNode(
        comid="123",
        point=GeoPoint(latitude=38.9, longitude=-77.0),
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
                series_refs=("series-123",),
            ),
        ),
    )


def valid_graph() -> HydrologyGraph:
    return HydrologyGraph(
        target_comid="123",
        coverage=TopologyCoverage.COMPLETE,
        nodes=(target_node(),),
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


def rebuild(graph: HydrologyGraph, **changes) -> HydrologyGraph:
    payload = graph.model_dump()
    payload.update(changes)
    return HydrologyGraph.model_validate(payload)


def test_minimal_graph_round_trips_with_api_array_layers() -> None:
    graph = valid_graph()

    restored = HydrologyGraph.model_validate_json(graph.model_dump_json())

    assert restored == graph
    assert restored.model_dump()["execution_layers"] == (("123",),)


def test_graph_rejects_missing_references_and_noncanonical_order() -> None:
    graph = valid_graph()
    upstream = HydrologyNode(
        comid="200",
        point=None,
        depth=1,
        cumulative_distance_km=1.0,
        is_target=False,
        is_inlet=False,
        participation=HydrologyParticipation.ACTIVE,
    )

    with pytest.raises(ValidationError, match="stable graph order"):
        rebuild(graph, nodes=(upstream, target_node()))

    with pytest.raises(ValidationError, match="edge endpoints"):
        rebuild(
            graph,
            edges=(
                HydrologyEdge(
                    upstream_comid="999",
                    downstream_comid="123",
                    distance_km=1.0,
                    participating=False,
                ),
            ),
        )


def test_graph_rejects_cycle_and_invalid_active_layers() -> None:
    graph = valid_graph()
    upstream = HydrologyNode(
        comid="200",
        point=None,
        depth=1,
        cumulative_distance_km=1.0,
        is_target=False,
        is_inlet=False,
        participation=HydrologyParticipation.ACTIVE,
    )
    target = target_node().model_copy(update={"is_inlet": False, "qualified_units": (), "station_refs": ()})
    nodes = (target, upstream)

    with pytest.raises(ValidationError, match="acyclic"):
        rebuild(
            graph,
            coverage=TopologyCoverage.NONE,
            nodes=nodes,
            edges=(
                HydrologyEdge(
                    upstream_comid="123",
                    downstream_comid="200",
                    distance_km=1.0,
                    participating=False,
                ),
                HydrologyEdge(
                    upstream_comid="200",
                    downstream_comid="123",
                    distance_km=1.0,
                    participating=False,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="execution layers"):
        rebuild(graph, execution_layers=(ComidLayer(("200",)),))


def test_node_requires_inlet_metadata_and_excluded_reason() -> None:
    with pytest.raises(ValidationError, match="station_refs"):
        target_node().model_copy(update={"station_refs": ()}).__class__.model_validate(
            target_node().model_copy(update={"station_refs": ()}).model_dump()
        )

    with pytest.raises(ValidationError, match="structured reasons"):
        HydrologyNode(
            comid="200",
            point=None,
            depth=1,
            cumulative_distance_km=1.0,
            is_target=False,
            is_inlet=False,
            participation=HydrologyParticipation.EXCLUDED,
        )
