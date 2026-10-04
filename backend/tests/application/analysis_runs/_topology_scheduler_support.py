import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.analysis_runs.models import (
    SessionResultReference,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
    StableSourceReference,
)
from app.domain import (
    AssessmentAvailability,
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
    RiskLevel,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologySeriesRef,
    TopologyStationRef,
)


RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
COMPLETED_AT = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
PARAMETER = ParameterKey(code_system="usgs", code="00010")
UNIT = CoverageUnit(parameter=PARAMETER, segment_index=0)


def topology(*paths: tuple[str, ...], max_nodes: int = 128) -> HydrologyGraph:
    if not paths or any(len(path) < 2 for path in paths):
        raise ValueError("scheduler topology paths must contain an edge")
    target = paths[0][-1]
    if any(path[-1] != target for path in paths):
        raise ValueError("scheduler topology paths must share a target")
    comids = {comid for path in paths for comid in path}
    depth = {
        comid: max(
            len(path) - index - 1
            for path in paths
            for index, item in enumerate(path)
            if item == comid
        )
        for comid in comids
    }
    roots = {path[0] for path in paths}
    nodes = tuple(
        sorted(
            (
                _node(
                    comid,
                    depth=depth[comid],
                    target=comid == target,
                    inlet=comid in roots,
                )
                for comid in comids
            ),
            key=lambda node: (
                node.depth,
                node.cumulative_distance_km,
                int(node.comid),
                node.comid,
            ),
        )
    )
    edge_pairs = {
        edge
        for path in paths
        for edge in zip(path, path[1:])
    }
    edges = tuple(
        HydrologyEdge(
            upstream_comid=upstream,
            downstream_comid=downstream,
            distance_km=1,
            participating=True,
        )
        for upstream, downstream in sorted(
            edge_pairs,
            key=lambda edge: (int(edge[0]), edge[0], int(edge[1]), edge[1]),
        )
    )
    layers = _execution_layers(comids, edge_pairs, nodes)
    outcomes = tuple(
        sorted(
            (
                BranchOutcome(
                    branch_root_comid=path[0],
                    coverage_unit=UNIT,
                    outcome=BranchOutcomeStatus.QUALIFIED,
                    stop_comid=path[0],
                    path_to_target=path,
                    reason=BranchOutcomeReason.STATION_QUALIFIED,
                )
                for path in paths
            ),
            key=lambda item: (
                item.coverage_unit.segment_index,
                item.coverage_unit.parameter.code_system,
                item.coverage_unit.parameter.code,
                int(item.branch_root_comid),
                item.branch_root_comid,
                len(item.path_to_target),
                tuple((int(comid), comid) for comid in item.path_to_target),
                item.outcome.value,
                -1,
                "",
                item.reason.value,
            ),
        )
    )
    return HydrologyGraph(
        target_comid=target,
        coverage=TopologyCoverage.COMPLETE,
        nodes=nodes,
        edges=edges,
        branch_outcomes=outcomes,
        execution_layers=layers,
        limits=TopologyLimits(
            max_distance_km=100,
            max_hops=max(12, max(depth.values())),
            max_nodes=max_nodes,
        ),
        stats=TopologySearchStats(
            discovered_nodes=len(nodes),
            queried_nodes=len(nodes),
            max_depth=max(depth.values()),
            max_distance_km=float(max(depth.values())),
            qualified_inlets=len(roots),
            excluded_nodes=0,
        ),
    )


def large_topology() -> HydrologyGraph:
    return topology(
        *((str(comid), "1000") for comid in range(1, 128)),
        max_nodes=128,
    )


def outcome(
    comid: str,
    status: DependencyStatus = DependencyStatus.INSUFFICIENT,
    *,
    run_id: UUID = RUN_ID,
) -> DependencyOutcome:
    session_id = uuid5(NAMESPACE_URL, f"scheduler:{run_id}:{comid}:session")
    if status in {DependencyStatus.USABLE, DependencyStatus.INSUFFICIENT}:
        result_item_id = uuid5(
            NAMESPACE_URL,
            f"scheduler:{run_id}:{comid}:result",
        )
        result_ref = SessionResultReference(
            run_id=run_id,
            item_id=result_item_id,
        )
        result_summary = WaterbodySessionResultSummary(
            result_id=f"result:{comid}",
            target_comid=comid,
            risk_level=RiskLevel.INDETERMINATE,
            availability=AssessmentAvailability.INDETERMINATE,
            overall_confidence=0,
        )
        usable_cell_refs = (
            (
                StableSourceReference(
                    run_id=run_id,
                    session_id=session_id,
                    item_id=result_item_id,
                    content_type="inference_result",
                    content_schema_version=1,
                    result_id=f"result:{comid}",
                    cell_id=f"cell:{comid}",
                    window_id="window-000",
                ),
            )
            if status is DependencyStatus.USABLE
            else ()
        )
    else:
        result_ref = None
        result_summary = None
        usable_cell_refs = ()
    return DependencyOutcome(
        run_id=run_id,
        target_comid=comid,
        session_id=session_id,
        status=status,
        result_ref=result_ref,
        result_summary=result_summary,
        usable_cell_refs=usable_cell_refs,
        completed_at=COMPLETED_AT,
    )


class RecordingOutcomePort:
    def __init__(self) -> None:
        self.outcomes: list[DependencyOutcome] = []

    async def publish(self, value: DependencyOutcome) -> None:
        if any(item.target_comid == value.target_comid for item in self.outcomes):
            raise AssertionError("scheduler published a node outcome twice")
        self.outcomes.append(value)


class ConcurrencyProbe:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.current = 0
        self.peak = 0
        self.reached = asyncio.Event()

    def enter(self) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)
        if self.current >= self.expected:
            self.reached.set()

    def leave(self) -> None:
        self.current -= 1


class RecordingRunner:
    def __init__(
        self,
        *,
        statuses: dict[str, DependencyStatus] | None = None,
        failures: set[str] | None = None,
        expected_overlap: int | None = None,
        release: asyncio.Event | None = None,
        concurrency_probe: ConcurrencyProbe | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.failures = failures or set()
        self.expected_overlap = expected_overlap
        self.release = release
        self.concurrency_probe = concurrency_probe
        self.requests = []
        self.failure_requests = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self.overlap_reached = asyncio.Event()

    async def run(self, request):
        self.requests.append(request)
        self.in_flight += 1
        if self.concurrency_probe is not None:
            self.concurrency_probe.enter()
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        if self.expected_overlap is not None and (
            self.in_flight >= self.expected_overlap
        ):
            self.overlap_reached.set()
        try:
            if self.release is not None:
                await self.release.wait()
            else:
                await asyncio.sleep(0)
            if request.node.comid in self.failures:
                raise RuntimeError("private runner failure")
            return outcome(
                request.node.comid,
                self.statuses.get(
                    request.node.comid,
                    DependencyStatus.INSUFFICIENT,
                ),
                run_id=request.run_id,
            )
        finally:
            self.in_flight -= 1
            if self.concurrency_probe is not None:
                self.concurrency_probe.leave()

    async def settle_failure(self, request, error):
        assert str(error) == "private runner failure"
        self.failure_requests.append(request)
        return outcome(
            request.node.comid,
            DependencyStatus.INVALID,
            run_id=request.run_id,
        )


def _node(
    comid: str,
    *,
    depth: int,
    target: bool,
    inlet: bool,
) -> HydrologyNode:
    station_refs = (
        (
            TopologyStationRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id=f"USGS-{comid}",
                series_refs=(
                    TopologySeriesRef(
                        series_identity=f"series-{comid}-00010",
                        parameter=PARAMETER,
                        channel="continuous",
                        covered_segment_indexes=(0,),
                    ),
                ),
            ),
        )
        if inlet
        else ()
    )
    return HydrologyNode(
        comid=comid,
        point=GeoPoint(latitude=39 + depth / 1000, longitude=-77),
        depth=depth,
        cumulative_distance_km=float(depth),
        is_target=target,
        is_inlet=inlet,
        participation=HydrologyParticipation.ACTIVE,
        qualified_units=(UNIT,) if inlet else (),
        station_refs=station_refs,
    )


def _execution_layers(
    comids: set[str],
    edges: set[tuple[str, str]],
    nodes: tuple[HydrologyNode, ...],
) -> tuple[ComidLayer, ...]:
    positions = {node.comid: index for index, node in enumerate(nodes)}
    remaining = {
        comid: sum(downstream == comid for _, downstream in edges)
        for comid in comids
    }
    downstreams = {
        comid: tuple(
            downstream
            for upstream, downstream in edges
            if upstream == comid
        )
        for comid in comids
    }
    layers = []
    pending = set(comids)
    while pending:
        ready = tuple(
            sorted(
                (comid for comid in pending if remaining[comid] == 0),
                key=positions.__getitem__,
            )
        )
        if not ready:
            raise ValueError("scheduler fixture contains a cycle")
        layers.append(ComidLayer(ready))
        pending.difference_update(ready)
        for comid in ready:
            for downstream in downstreams[comid]:
                remaining[downstream] -= 1
    return tuple(layers)
