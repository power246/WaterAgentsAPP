import asyncio
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
from app.application.analysis_runs.workflow import CancellationToken
from app.application.evidence_tools.authorization import build_water_observation_plans
from app.application.evidence_tools.numeric_dependency import prepare_numeric_scope
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
)
from app.application.evidence_tools.scope import assemble_evidence_session_scope
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
    EvidenceToolRequest,
    GeoPoint,
    HydrologyParticipation,
    MemoryMode,
    ModelSelection,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    ReferenceScope,
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
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import InvalidPortResponseError, PortRateLimitError, PortTimeoutError

DECISION = datetime(2026, 9, 25, 8, tzinfo=UTC)
TARGET = "100"
OXYGEN = ParameterKey(code_system="usgs", code="00300")
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
TEMPERATURE = ParameterKey(code_system="usgs", code="00010")
CONDUCTANCE = ParameterKey(code_system="usgs", code="00095")


def run(coroutine):
    return asyncio.run(coroutine)


def context(
    *,
    end_date: date = date(2026, 9, 24),
    decision: datetime = DECISION,
) -> AnalysisContext:
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
        decision_instant=decision,
        analysis_timezone="America/New_York",
        resolution_preference=TimeResolutionPreference.AUTO,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )
    assert dates.temporal is not None
    temporal = dates.temporal
    return AnalysisContext(
        query="Was dissolved oxygen safe yesterday?",
        decision_instant=decision,
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
        coverage: dict[str, set[tuple[str, int] | tuple[str, int, int]]],
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
            if (
                (unit.parameter.code, unit.segment_index)
                in self.coverage.get(request.comid, set())
                or (
                    unit.window_index is not None
                    and (
                        unit.parameter.code,
                        unit.segment_index,
                        unit.window_index,
                    )
                    in self.coverage.get(request.comid, set())
                )
            )
        )
        if not selected:
            return ()
        station_id = f"USGS-{request.comid}"
        references = tuple(
            SeriesRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id=station_id,
                parameter=parameter,
                channel="continuous",
                series_identity=(
                    f"series-{request.comid}-{parameter.code}-{source.value}"
                ),
                coverage_source=source,
                covered_units=tuple(
                    unit
                    for unit in selected
                    if unit.parameter == parameter
                    and (unit.window_index is not None)
                    == (source is AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION)
                ),
            )
            for parameter in sorted(
                {unit.parameter for unit in selected},
                key=lambda item: (item.code_system, item.code),
            )
            for source in (
                AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION,
                AvailabilityCoverageSource.PREDICTION_TIME_SERIES_BASELINE,
            )
            if any(
                unit.parameter == parameter
                and (unit.window_index is not None)
                == (source is AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION)
                for unit in selected
            )
        )
        references = tuple(
            sorted(
                references,
                key=lambda reference: (
                    reference.provider.value,
                    reference.parameter.code_system,
                    reference.parameter.code,
                    reference.coverage_source.value,
                    reference.series_identity,
                ),
            )
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
    numeric_registry: NumericMethodRegistry | None = None,
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
        numeric_registry=numeric_registry,
        capability_registry=(
            load_parameter_capability_registry() if numeric_registry else None
        ),
    )


def test_method_input_only_station_reaches_m5_without_qualifying_target() -> None:
    methods = NumericMethodRegistry(
        (
            NumericMethod(
                method_id="test:oxygen_from_local_inputs",
                version=1,
                output_parameter=OXYGEN,
                output_unit="mg/l",
                inputs=(
                    NumericMethodInput(parameter=TEMPERATURE, unit="deg C"),
                    NumericMethodInput(parameter=CONDUCTANCE, unit="uS/cm"),
                ),
                calculate=lambda values, _windows, _target: sum(values),
                confidence_cap=0.5,
            ),
        )
    )
    inventory = FakeInventory(
        {
            TARGET: {
                (code, 0, window_index)
                for code in (TEMPERATURE.code, CONDUCTANCE.code)
                for window_index in range(4)
            },
        }
    )
    graph = run(
        builder(FakeTopology({}), inventory, numeric_registry=methods)
        .build(context())
    )
    target = next(node for node in graph.nodes if node.comid == TARGET)
    assert graph.coverage is TopologyCoverage.NONE
    assert target.participation is HydrologyParticipation.ACTIVE
    assert not target.is_inlet
    assert target.qualified_units == ()
    assert {
        reference.parameter
        for station in target.station_refs
        for reference in station.series_refs
    } == {TEMPERATURE, CONDUCTANCE}
    assert {item.coverage_unit.parameter for item in graph.branch_outcomes} == {
        OXYGEN,
        DISCHARGE,
    }

    scope = assemble_evidence_session_scope(
        run_id="run-input-only",
        session_id="session-input-only",
        session_fact=ReferenceScope(
            item_id="session", run_id="run-input-only", session_id="session-input-only"
        ),
        analysis_context_ref=ReferenceScope(item_id="context", run_id="run-input-only"),
        topology_ref=ReferenceScope(item_id="topology", run_id="run-input-only"),
        context=context(),
        topology=graph,
        agent_comid=TARGET,
        registry=load_parameter_capability_registry(),
        cancel_token=CancellationToken(asyncio.Event()),
    )
    scope = prepare_numeric_scope(scope, methods)
    assert scope.intermediate_parameters == (TEMPERATURE, CONDUCTANCE)
    plans = build_water_observation_plans(
        scope,
        EvidenceToolRequest(
            tool_call_id="collect-method-inputs",
            session_id=scope.session_id,
            tool_name="collect_water_observations",
            purpose="Collect registered method inputs.",
            window_ids=tuple(window.window_id for window in scope.time_windows),
            parameter_keys=scope.intermediate_parameters,
        ),
        operation_id="collect-method-inputs",
        registry=load_parameter_capability_registry(),
    )
    assert {plan.parameter_keys[0] for plan in plans} == {
        TEMPERATURE,
        CONDUCTANCE,
    }

    production_methods = NumericMethodRegistry(
        (replace(methods.methods[0], production=True),)
    )
    production_graph = run(
        builder(FakeTopology({}), inventory, numeric_registry=production_methods)
        .build(context())
    )
    assert {
        reference.parameter
        for station in production_graph.nodes[0].station_refs
        for reference in station.series_refs
    } == {TEMPERATURE, CONDUCTANCE}


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


def test_target_historical_candidate_checks_for_one_backup() -> None:
    topology = FakeTopology({})
    inventory = FakeInventory({TARGET: codes("00060", "00300")})

    graph = run(builder(topology, inventory).build(context()))

    assert graph.coverage is TopologyCoverage.COMPLETE
    assert [node.comid for node in graph.nodes] == [TARGET]
    assert graph.nodes[0].is_inlet
    assert graph.execution_layers[0].root == (TARGET,)
    assert topology.segment_calls == [TARGET]
    assert topology.calls == [(TARGET, 100.0)]
    assert set(statuses(graph, BranchOutcomeStatus.BACKUP_SEARCH_ENDED)) == {
        BranchOutcomeReason.NATURAL_HEADWATER
    }


def test_historical_candidate_search_keeps_one_upstream_alternative() -> None:
    topology = FakeTopology(linear_topology(3))
    inventory = FakeInventory(
        {
            "200": codes("00060", "00300"),
            "201": codes("00060", "00300"),
            "202": codes("00060", "00300"),
        }
    )

    graph = run(
        builder(topology, inventory).build(
            context(decision=datetime(2026, 9, 27, 8, tzinfo=UTC))
        )
    )

    assert {node.comid for node in graph.nodes if node.is_inlet} == {"200", "201"}
    assert "202" not in {node.comid for node in graph.nodes}
    assert graph.coverage is TopologyCoverage.COMPLETE
    assert graph.stats.qualified_inlets == 2
    assert topology.calls == [(TARGET, 100.0), ("200", 100.0)]


def test_historical_qualification_and_station_refs_are_window_specific() -> None:
    topology = FakeTopology({TARGET: (segment("200", TARGET),)})
    inventory = FakeInventory(
        {
            TARGET: {(code, 0, 0) for code in ("00060", "00300")},
            "200": {
                (code, 0, window_index)
                for code in ("00060", "00300")
                for window_index in (1, 2, 3)
            },
        }
    )

    graph = run(builder(topology, inventory).build(context()))

    assert graph.coverage is TopologyCoverage.COMPLETE
    target = next(node for node in graph.nodes if node.comid == TARGET)
    upstream = next(node for node in graph.nodes if node.comid == "200")
    assert {unit.window_index for unit in target.qualified_units} == {0}
    assert {unit.window_index for unit in upstream.qualified_units} == {1, 2, 3}
    assert {
        window_id
        for station in target.station_refs
        for reference in station.series_refs
        for window_id in reference.covered_window_ids
    } == {"window-000"}
    assert {
        window_id
        for station in upstream.station_refs
        for reference in station.series_refs
        for window_id in reference.covered_window_ids
    } == {"window-001", "window-002", "window-003"}
    assert all(
        reference.covered_segment_indexes == (0,)
        for node in (target, upstream)
        for station in node.station_refs
        for reference in station.series_refs
    )


def test_mixed_prediction_stops_at_first_candidate_while_history_continues() -> None:
    topology = FakeTopology({TARGET: (segment("200", TARGET),)})
    inventory = FakeInventory(
        {
            TARGET: codes("00060", "00300", segment_index=1),
            "200": codes("00060", "00300", segment_index=0),
        }
    )

    graph = run(
        builder(topology, inventory).build(context(end_date=date(2026, 9, 25)))
    )

    target = next(node for node in graph.nodes if node.comid == TARGET)
    upstream = next(node for node in graph.nodes if node.comid == "200")
    assert graph.coverage is TopologyCoverage.COMPLETE
    assert {unit.window_index for unit in target.qualified_units} == {None}
    assert {unit.window_index for unit in upstream.qualified_units} == {0, 1, 2, 3}
    assert all(
        reference.covered_window_ids is None
        for station in target.station_refs
        for reference in station.series_refs
    )
    assert topology.calls == [(TARGET, 100.0), ("200", 100.0)]


def test_missing_historical_alternative_does_not_downgrade_qualified_branch() -> None:
    topology = FakeTopology({TARGET: (segment("200", TARGET),), "200": (segment("201", "200"),)})
    inventory = FakeInventory({"200": codes("00060", "00300")})

    graph = run(
        builder(topology, inventory).build(
            context(decision=datetime(2026, 9, 27, 8, tzinfo=UTC))
        )
    )

    assert graph.coverage is TopologyCoverage.COMPLETE
    assert set(statuses(graph, BranchOutcomeStatus.BACKUP_SEARCH_ENDED)) == {
        BranchOutcomeReason.NATURAL_HEADWATER
    }
    node_201 = next(node for node in graph.nodes if node.comid == "201")
    assert node_201.participation is HydrologyParticipation.EXCLUDED
    assert {reason.code.value for reason in node_201.excluded_reasons} == {
        "backup_search_ended"
    }


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


def test_every_historical_window_and_prediction_segment_have_required_parameters() -> None:
    inventory = FakeInventory({})

    run(builder(FakeTopology({}), inventory).build(context(end_date=date(2026, 9, 25))))

    request = inventory.requests[0]
    assert len(request.historical_ranges) == 4
    assert len(request.prediction_baseline_ranges) == 1
    assert {
        (unit.segment_index, unit.window_index, unit.parameter.code)
        for unit in request.coverage_units
    } == {
        *((0, index, code) for index in range(4) for code in ("00060", "00300")),
        (1, None, "00060"),
        (1, None, "00300"),
    }


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
    ) == 16
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


def test_provider_failure_and_deadline_are_not_published_as_partial_graph(caplog) -> None:
    failure = PortRateLimitError(
        "limited",
        provider="fixture",
        operation="get_time_series_observation_windows",
        status_code=429,
        retry_after_seconds=3267,
    )
    with (
        caplog.at_level(logging.WARNING, logger="wateragents.hydrology_topology"),
        pytest.raises(PortRateLimitError) as captured,
    ):
        run(
            builder(
                FakeTopology({}),
                FakeInventory({}, error=failure),
            ).build(context())
        )
    assert captured.value is failure
    assert failure.operation == "check_availability"
    [record] = [
        item for item in caplog.records
        if item.name == "wateragents.hydrology_topology"
    ]
    assert record.comid == TARGET
    assert record.operation == "check_availability"
    assert record.provider == "fixture"
    assert record.provider_operation == "get_time_series_observation_windows"
    assert record.provider_error == "PortRateLimitError"
    assert record.provider_status == 429
    assert record.provider_retry_after_seconds == 3267
    assert f"COMID {TARGET}" in record.getMessage()
    assert "limited" in record.getMessage()

    slow_probe = ConcurrencyProbe({TARGET: 0.02})
    with pytest.raises(PortTimeoutError):
        run(
            builder(
                FakeTopology({}, probe=slow_probe),
                FakeInventory({}, probe=slow_probe),
                timeout=0.001,
            ).build(context())
        )


def test_invalid_availability_result_logs_the_queried_comid(caplog) -> None:
    class WrongComidInventory(FakeInventory):
        async def check_availability(
            self,
            request: StationAvailabilityRequest,
            *,
            deadline: float | None = None,
        ) -> tuple[StationAvailability, ...]:
            valid = await super().check_availability(request, deadline=deadline)
            return (valid[0].model_copy(update={"comid": "200"}),)

    with (
        caplog.at_level(logging.WARNING, logger="wateragents.hydrology_topology"),
        pytest.raises(InvalidPortResponseError, match="different COMID"),
    ):
        run(
            builder(
                FakeTopology({}),
                WrongComidInventory({TARGET: codes("00060", "00300")}),
            ).build(context())
        )

    [record] = [
        item for item in caplog.records
        if item.name == "wateragents.hydrology_topology"
    ]
    assert (record.comid, record.operation) == (TARGET, "check_availability")
    assert record.provider_error == "InvalidPortResponseError"
    assert f"COMID {TARGET}" in record.getMessage()


def test_invalid_upstream_result_logs_the_queried_comid(caplog) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="wateragents.hydrology_topology"),
        pytest.raises(InvalidPortResponseError, match="downstream COMID"),
    ):
        run(
            builder(
                FakeTopology({TARGET: (segment("200", "999"),)}),
                FakeInventory({}),
            ).build(context())
        )

    [record] = [
        item for item in caplog.records
        if item.name == "wateragents.hydrology_topology"
    ]
    assert (record.comid, record.operation) == (TARGET, "get_direct_upstreams")
    assert record.provider_error == "InvalidPortResponseError"
    assert f"COMID {TARGET}" in record.getMessage()


def test_invalid_target_segment_logs_the_queried_comid(caplog) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="wateragents.hydrology_topology"),
        pytest.raises(InvalidPortResponseError, match="different COMID"),
    ):
        run(
            builder(
                FakeTopology({}, target_segment=segment("200", TARGET)),
                FakeInventory({}),
            ).build(context())
        )

    [record] = [
        item for item in caplog.records
        if item.name == "wateragents.hydrology_topology"
    ]
    assert (record.comid, record.operation) == (TARGET, "get_segment")
    assert record.provider_error == "InvalidPortResponseError"
    assert f"COMID {TARGET}" in record.getMessage()
