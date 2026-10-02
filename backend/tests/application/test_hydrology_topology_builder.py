import asyncio
from collections.abc import Iterable
from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from app.application.hydrology_topology import (
    HydrologyGraphBuilder,
    TopologyInvariantError,
)
from app.application.query_understanding import (
    AnalysisContext,
    DateResolver,
    ParameterConcept,
    ParameterIntentSource,
    ParsedIntentV1,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
)
from app.domain import (
    AvailabilityCoverageSource,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    CoverageUnit,
    GeoPoint,
    HydrologyParticipation,
    MemoryMode,
    ModelSelection,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    RiverSegment,
    SeriesRef,
    StationAvailability,
    StationAvailabilityRequest,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TopologyCoverage,
    TopologyLimits,
    WaterbodyGeometry,
)
from app.ports import InvalidPortResponseError, PortRateLimitError, PortTimeoutError

UTC = timezone.utc
DECISION = datetime(2026, 9, 25, 8, tzinfo=UTC)
TARGET = "100"
OXYGEN = ParameterKey(code_system="usgs", code="00300")
DISCHARGE = ParameterKey(code_system="usgs", code="00060")


def run(coroutine):
    return asyncio.run(coroutine)


def context(*, end_date: date = date(2026, 9, 24)) -> AnalysisContext:
    parsed = ParsedIntentV1(
        start_date=date(2026, 9, 24),
        end_date=end_date,
        parameters=(
            ParameterConcept(
                concept="dissolved oxygen",
                rationale="Requested indicator.",
                explicitly_named=True,
            ),
        ),
    )
    dates = DateResolver().resolve(
        None,
        parsed,
        decision_instant=DECISION,
        analysis_timezone="America/New_York",
        resolution_preference=TimeResolutionPreference.AUTO,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )
    assert dates.temporal is not None
    temporal = dates.temporal
    return AnalysisContext(
        query="Was dissolved oxygen safe yesterday?",
        decision_instant=DECISION,
        analysis_timezone="America/New_York",
        target=ResolvedTarget(
            comid=TARGET,
            point=GeoPoint(latitude=38.9, longitude=-77.0),
            display_name="Target reach",
            country_code="US",
            subdivision_code="US-DC",
            source=TargetSource.QUERY,
        ),
        requested_date_range=temporal.requested_date_range,
        time_range=temporal.time_range,
        time_segments=temporal.time_segments,
        mode=temporal.mode,
        time_resolution=temporal.time_resolution,
        time_resolution_source=temporal.time_resolution_source,
        params_used=(
            ResolvedParameterIntent(
                key=OXYGEN,
                canonical_name="Dissolved oxygen",
                canonical_unit="mg/l",
                source=ParameterIntentSource.EXPLICIT,
                rationale="Requested indicator.",
            ),
        ),
        supporting_params=(
            SupportingParameterIntent(
                key=DISCHARGE,
                canonical_name="Discharge",
                canonical_unit="ft3/s",
                reason=SupportingIntentReason.CAPABILITY_REQUIRED,
            ),
        ),
        parser_model=ModelSelection(provider="openai", model="parser"),
        waterbody_model=ModelSelection(provider="openai", model="waterbody"),
        memory_mode=MemoryMode.STATELESS,
    )


def segment(
    comid: str,
    downstream: str,
    *,
    distance: float = 1.0,
    latitude: float | None = None,
) -> RiverSegment:
    return RiverSegment(
        comid=comid,
        location=GeoPoint(
            latitude=latitude if latitude is not None else 38.0 + int(comid) / 10000,
            longitude=-77.0,
        ),
        distance_to_downstream_km=distance,
        downstream_comids=(downstream,),
    )


class ConcurrencyProbe:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self.delays = delays or {}
        self.active = 0
        self.peak = 0

    async def wait(self, comid: str) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delays.get(comid, 0))
        finally:
            self.active -= 1


class FakeTopology:
    def __init__(
        self,
        upstreams: dict[str, tuple[RiverSegment, ...]],
        *,
        probe: ConcurrencyProbe | None = None,
        error: Exception | None = None,
        target_segment: RiverSegment | None = None,
    ) -> None:
        self.upstreams = upstreams
        self.probe = probe or ConcurrencyProbe()
        self.error = error
        self.target_segment = target_segment
        self.calls: list[tuple[str, float]] = []
        self.segment_calls: list[str] = []

    async def get_segment(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> RiverSegment | None:
        del deadline
        self.segment_calls.append(comid)
        return self.target_segment

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
        *,
        deadline: float | None = None,
    ) -> tuple[RiverSegment, ...]:
        self.calls.append((comid, max_distance_km))
        await self.probe.wait(comid)
        if self.error is not None:
            raise self.error
        return self.upstreams.get(comid, ())


class FakeInventory:
    def __init__(
        self,
        coverage: dict[str, set[tuple[str, int]]],
        *,
        probe: ConcurrencyProbe | None = None,
        error: Exception | None = None,
    ) -> None:
        self.coverage = coverage
        self.probe = probe or ConcurrencyProbe()
        self.error = error
        self.calls: list[str] = []
        self.requests: list[StationAvailabilityRequest] = []

    async def check_availability(
        self,
        request: StationAvailabilityRequest,
        *,
        deadline: float | None = None,
    ) -> tuple[StationAvailability, ...]:
        self.calls.append(request.comid)
        self.requests.append(request)
        await self.probe.wait(request.comid)
        if self.error is not None:
            raise self.error
        selected = tuple(
            unit
            for unit in request.coverage_units
            if (unit.parameter.code, unit.segment_index)
            in self.coverage.get(request.comid, set())
        )
        if not selected:
            return ()
        station_id = f"USGS-{request.comid}"
        historical_indexes = {
            item.segment_index for item in request.historical_ranges
        }
        references = tuple(
            SeriesRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id=station_id,
                parameter=unit.parameter,
                channel="continuous",
                series_identity=(
                    f"series-{request.comid}-{unit.parameter.code}-{unit.segment_index}"
                ),
                coverage_source=(
                    AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_ENVELOPE
                    if unit.segment_index in historical_indexes
                    else AvailabilityCoverageSource.PREDICTION_TIME_SERIES_BASELINE
                ),
                covered_units=(unit,),
            )
            for unit in selected
        )
        return (
            StationAvailability(
                station=MonitoringStation(
                    station_id=station_id,
                    source="nwissite",
                    name=f"Station {request.comid}",
                    location=None,
                    associated_comid=request.comid,
                ),
                comid=request.comid,
                providers=(ParameterProvider.WDFN_TIME_SERIES,),
                covered_units=selected,
                series_refs=references,
                checked_at=DECISION,
            ),
        )


def builder(
    topology: FakeTopology,
    inventory: FakeInventory,
    *,
    limits: TopologyLimits | None = None,
    max_concurrency: int = 8,
    timeout: float = 1,
) -> HydrologyGraphBuilder:
    return HydrologyGraphBuilder(
        topology=topology,
        station_inventory=inventory,
        limits=limits
        or TopologyLimits(
            max_distance_km=100.0,
            max_hops=12,
            max_nodes=128,
        ),
        max_concurrency=max_concurrency,
        stage_timeout_seconds=timeout,
    )


def codes(*values: str, segment_index: int = 0) -> set[tuple[str, int]]:
    return {(value, segment_index) for value in values}


def linear_topology(
    hops: int,
    *,
    distance: float = 1.0,
) -> dict[str, tuple[RiverSegment, ...]]:
    result: dict[str, tuple[RiverSegment, ...]] = {}
    downstream = TARGET
    for offset in range(hops):
        upstream = str(200 + offset)
        result[downstream] = (
            segment(upstream, downstream, distance=distance),
        )
        downstream = upstream
    return result


def statuses(graph, status: BranchOutcomeStatus) -> list[BranchOutcomeReason]:
    return [
        outcome.reason
        for outcome in graph.branch_outcomes
        if outcome.outcome is status
    ]


def test_target_complete_stops_without_topology_navigation() -> None:
    topology = FakeTopology({})
    inventory = FakeInventory({TARGET: codes("00060", "00300")})

    graph = run(builder(topology, inventory).build(context()))

    assert graph.coverage is TopologyCoverage.COMPLETE
    assert [node.comid for node in graph.nodes] == [TARGET]
    assert graph.nodes[0].is_inlet
    assert graph.execution_layers[0].root == (TARGET,)
    assert topology.segment_calls == [TARGET]
    assert topology.calls == []


def test_target_geometry_is_stored_once_with_resolved_node_summary() -> None:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid=TARGET,
        coordinates=((-77.1, 38.8), (-77.0, 38.9)),
    )
    topology = FakeTopology(
        {},
        target_segment=RiverSegment(
            comid=TARGET,
            location=geometry.representative_point,
            geometry_ref=geometry.geometry_ref,
            geometry_summary=geometry.summary(),
            geometry=geometry,
        ),
    )
    inventory = FakeInventory({TARGET: codes("00060", "00300")})

    graph = run(builder(topology, inventory).build(context()))

    assert graph.geometries == {geometry.geometry_ref: geometry}
    assert graph.nodes[0].geometry_ref == geometry.geometry_ref
    assert graph.nodes[0].geometry_summary == geometry.summary()
    assert graph.model_dump_json().count('"full_coordinates"') == 1


def test_every_segment_contains_deduplicated_discharge_and_target_parameter() -> None:
    inventory = FakeInventory({})

    run(builder(FakeTopology({}), inventory).build(context(end_date=date(2026, 9, 25))))

    request = inventory.requests[0]
    assert [
        (unit.segment_index, unit.parameter.code)
        for unit in request.coverage_units
    ] == [
        (0, "00060"),
        (0, "00300"),
        (1, "00060"),
        (1, "00300"),
    ]


def test_multibranch_partial_station_coverage_continues_only_missing_units() -> None:
    topology = FakeTopology(
        {
            TARGET: (segment("300", TARGET), segment("200", TARGET)),
            "200": (segment("400", "200"),),
        }
    )
    inventory = FakeInventory(
        {
            "200": codes("00300"),
            "300": codes("00060", "00300"),
            "400": codes("00060"),
        }
    )

    graph = run(builder(topology, inventory).build(context()))

    assert graph.coverage is TopologyCoverage.COMPLETE
    assert {node.comid for node in graph.nodes if node.is_inlet} == {"200", "300", "400"}
    node_200 = next(node for node in graph.nodes if node.comid == "200")
    node_400 = next(node for node in graph.nodes if node.comid == "400")
    assert {unit.parameter for unit in node_200.qualified_units} == {OXYGEN}
    assert {unit.parameter for unit in node_400.qualified_units} == {DISCHARGE}
    assert inventory.calls.count("200") == 1
    assert all(edge.participating for edge in graph.edges)


def test_natural_empty_is_not_limit_and_target_remains_active() -> None:
    topology = FakeTopology({TARGET: (segment("200", TARGET),)})
    inventory = FakeInventory({})

    graph = run(builder(topology, inventory).build(context()))

    assert graph.coverage is TopologyCoverage.NONE
    assert set(statuses(graph, BranchOutcomeStatus.NO_QUALIFYING_SOURCE)) == {
        BranchOutcomeReason.NATURAL_HEADWATER
    }
    assert statuses(graph, BranchOutcomeStatus.SEARCH_LIMIT_REACHED) == []
    assert graph.execution_layers[-1].root == (TARGET,)
    assert graph.nodes[0].participation is HydrologyParticipation.ACTIVE
    assert next(node for node in graph.nodes if node.comid == "200").excluded_reasons


@pytest.mark.parametrize(
    ("limits", "topology_data", "expected_reason"),
    [
        (
            TopologyLimits(max_distance_km=1.0, max_hops=12, max_nodes=128),
            {TARGET: (segment("200", TARGET, distance=1.1),)},
            BranchOutcomeReason.MAX_DISTANCE_REACHED,
        ),
        (
            TopologyLimits(max_distance_km=100.0, max_hops=1, max_nodes=128),
            {
                TARGET: (segment("200", TARGET),),
                "200": (segment("300", "200"),),
            },
            BranchOutcomeReason.MAX_HOPS_REACHED,
        ),
        (
            TopologyLimits(max_distance_km=100.0, max_hops=12, max_nodes=2),
            {TARGET: (segment("300", TARGET), segment("200", TARGET))},
            BranchOutcomeReason.MAX_NODES_REACHED,
        ),
    ],
)
def test_each_hard_limit_has_a_distinct_search_limit_outcome(
    limits: TopologyLimits,
    topology_data: dict[str, tuple[RiverSegment, ...]],
    expected_reason: BranchOutcomeReason,
) -> None:
    graph = run(
        builder(
            FakeTopology(topology_data),
            FakeInventory({}),
            limits=limits,
        ).build(context())
    )

    assert expected_reason in statuses(
        graph,
        BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
    )
    assert graph.stats.discovered_nodes <= limits.max_nodes


def test_each_blocked_sibling_keeps_its_identity_at_distance_limit() -> None:
    graph = run(
        builder(
            FakeTopology(
                {
                    TARGET: (
                        segment("200", TARGET, distance=1.1),
                        segment("300", TARGET, distance=1.2),
                    )
                }
            ),
            FakeInventory({}),
            limits=TopologyLimits(
                max_distance_km=1.0,
                max_hops=12,
                max_nodes=128,
            ),
        ).build(context())
    )

    limited = tuple(
        outcome
        for outcome in graph.branch_outcomes
        if outcome.reason is BranchOutcomeReason.MAX_DISTANCE_REACHED
    )
    assert {outcome.blocked_upstream_comid for outcome in limited} == {
        "200",
        "300",
    }
    assert {outcome.stop_comid for outcome in limited} == {TARGET}
    assert {outcome.path_to_target for outcome in limited} == {(TARGET,)}


def test_exact_production_boundaries_stop_before_distance_hop_and_node_overflow() -> None:
    distance_graph = run(
        builder(
            FakeTopology(linear_topology(2, distance=100.0)),
            FakeInventory({}),
            limits=TopologyLimits(
                max_distance_km=100.0,
                max_hops=12,
                max_nodes=128,
            ),
        ).build(context())
    )
    assert distance_graph.stats.max_distance_km == 100.0
    assert distance_graph.stats.discovered_nodes == 2
    assert BranchOutcomeReason.MAX_DISTANCE_REACHED in statuses(
        distance_graph,
        BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
    )

    hop_graph = run(
        builder(
            FakeTopology(linear_topology(13)),
            FakeInventory({}),
            limits=TopologyLimits(
                max_distance_km=100.0,
                max_hops=12,
                max_nodes=128,
            ),
        ).build(context())
    )
    assert hop_graph.stats.max_depth == 12
    assert hop_graph.stats.discovered_nodes == 13
    assert BranchOutcomeReason.MAX_HOPS_REACHED in statuses(
        hop_graph,
        BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
    )

    node_graph = run(
        builder(
            FakeTopology(
                {
                    TARGET: tuple(
                        segment(str(comid), TARGET)
                        for comid in range(200, 328)
                    )
                }
            ),
            FakeInventory({}),
            limits=TopologyLimits(
                max_distance_km=100.0,
                max_hops=12,
                max_nodes=128,
            ),
        ).build(context())
    )
    assert node_graph.stats.discovered_nodes == 128
    assert "327" not in {node.comid for node in node_graph.nodes}
    assert BranchOutcomeReason.MAX_NODES_REACHED in statuses(
        node_graph,
        BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
    )


def test_simultaneous_distance_and_hop_boundary_has_explicit_reason() -> None:
    graph = run(
        builder(
            FakeTopology(linear_topology(2)),
            FakeInventory({}),
            limits=TopologyLimits(
                max_distance_km=1.0,
                max_hops=1,
                max_nodes=128,
            ),
        ).build(context())
    )

    assert BranchOutcomeReason.MULTIPLE_LIMITS_REACHED in statuses(
        graph,
        BranchOutcomeStatus.SEARCH_LIMIT_REACHED,
    )


def test_shared_upstream_is_queried_once_without_swallowing_branch_outcomes() -> None:
    topology = FakeTopology(
        {
            TARGET: (segment("200", TARGET), segment("300", TARGET)),
            "200": (segment("400", "200"),),
            "300": (segment("400", "300"),),
        }
    )
    inventory = FakeInventory({"400": codes("00060", "00300")})

    graph = run(builder(topology, inventory).build(context()))

    assert inventory.calls.count("400") == 1
    assert len(
        [
            outcome
            for outcome in graph.branch_outcomes
            if outcome.stop_comid == "400"
            and outcome.outcome is BranchOutcomeStatus.QUALIFIED
        ]
    ) == 4
    assert {edge.downstream_comid for edge in graph.edges if edge.upstream_comid == "400"} == {
        "200",
        "300",
    }


@pytest.mark.parametrize(
    "topology_data",
    [
        {
            TARGET: (
                RiverSegment.model_construct(
                    comid=TARGET,
                    location=None,
                    distance_to_downstream_km=1.0,
                    upstream_comids=(),
                    downstream_comids=(TARGET,),
                ),
            )
        },
        {
            TARGET: (segment("200", TARGET),),
            "200": (segment(TARGET, "200"),),
        },
    ],
)
def test_cycle_and_self_edge_fail_the_stage(
    topology_data: dict[str, tuple[RiverSegment, ...]],
) -> None:
    with pytest.raises(TopologyInvariantError):
        run(builder(FakeTopology(topology_data), FakeInventory({})).build(context()))


def test_duplicate_records_are_deduplicated_but_conflicts_and_missing_distance_fail() -> None:
    duplicate = segment("200", TARGET)
    graph = run(
        builder(
            FakeTopology({TARGET: (duplicate, duplicate)}),
            FakeInventory({"200": codes("00060", "00300")}),
        ).build(context())
    )
    assert [node.comid for node in graph.nodes] == [TARGET, "200"]

    conflicting = segment("200", TARGET, latitude=39.0)
    with pytest.raises(InvalidPortResponseError, match="conflicting duplicates"):
        run(
            builder(
                FakeTopology({TARGET: (duplicate, conflicting)}),
                FakeInventory({}),
            ).build(context())
        )

    missing_distance = RiverSegment(
        comid="200",
        location=None,
        downstream_comids=(TARGET,),
    )
    with pytest.raises(InvalidPortResponseError, match="omitted edge distance"):
        run(
            builder(
                FakeTopology({TARGET: (missing_distance,)}),
                FakeInventory({}),
            ).build(context())
        )


def test_concurrency_is_bounded_and_random_completion_order_is_byte_stable() -> None:
    branches = tuple(segment(str(comid), TARGET) for comid in range(200, 212))

    def exercise(reverse: bool) -> tuple[str, int]:
        delays = {
            str(comid): (
                (211 - comid) if reverse else (comid - 200)
            )
            * 0.0002
            for comid in range(200, 212)
        }
        probe = ConcurrencyProbe(delays)
        topology = FakeTopology(
            {TARGET: tuple(reversed(branches)) if reverse else branches},
            probe=probe,
        )
        inventory = FakeInventory(
            {
                str(comid): codes("00060", "00300")
                for comid in range(200, 212)
            },
            probe=probe,
        )
        graph = run(
            builder(
                topology,
                inventory,
                max_concurrency=3,
            ).build(context())
        )
        return graph.model_dump_json(), probe.peak

    first, first_peak = exercise(False)
    second, second_peak = exercise(True)

    assert first == second
    assert sha256(first.encode()).digest() == sha256(second.encode()).digest()
    assert first_peak <= 3
    assert second_peak <= 3


def test_production_concurrency_cap_never_exceeds_eight_external_requests() -> None:
    branches = tuple(segment(str(comid), TARGET) for comid in range(200, 216))
    probe = ConcurrencyProbe(
        {str(comid): 0.01 for comid in range(200, 216)}
    )
    graph = run(
        builder(
            FakeTopology({TARGET: branches}, probe=probe),
            FakeInventory(
                {
                    str(comid): codes("00060", "00300")
                    for comid in range(200, 216)
                },
                probe=probe,
            ),
            max_concurrency=8,
        ).build(context())
    )

    assert graph.coverage is TopologyCoverage.COMPLETE
    assert probe.peak == 8


def test_provider_failure_and_deadline_are_not_published_as_partial_graph() -> None:
    failure = PortRateLimitError(
        "limited",
        provider="fixture",
        operation="availability",
    )
    with pytest.raises(PortRateLimitError) as captured:
        run(
            builder(
                FakeTopology({}),
                FakeInventory({}, error=failure),
            ).build(context())
        )
    assert captured.value is failure

    slow_probe = ConcurrencyProbe({TARGET: 0.02})
    with pytest.raises(PortTimeoutError):
        run(
            builder(
                FakeTopology({}, probe=slow_probe),
                FakeInventory({}, probe=slow_probe),
                timeout=0.001,
            ).build(context())
        )
