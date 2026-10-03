import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.application.analysis_runs.models import (
    SessionResultReference,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyDischargeState,
    DependencyOutcome,
    StableSourceReference,
)
from app.application.evidence_tools import (
    OutcomeAlreadyPublishedError,
    OutcomeIdentityError,
    OutcomesNotReadyError,
    ResolvedInferenceResult,
    RunLocalUpstreamResultStore,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    AnalysisDetail,
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
    InferenceResult,
    ItemReference,
    OutcomeStatus,
    ParameterKey,
    ParameterProvider,
    ResultSource,
    TopologyCoverage,
    TopologyLimits,
    TopologySearchStats,
    TopologyStationRef,
    TopologySeriesRef,
    TimeRange,
    TimeResolution,
    UpstreamResultRequest,
    WaterQualityParameter,
)
from tests.domain.test_results import local_day, make_result, make_series


RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_RUN_ID = UUID("10000000-0000-0000-0000-000000000002")
TOPOLOGY_ITEM_ID = UUID("20000000-0000-0000-0000-000000000001")
DOWNSTREAM_SESSION_ID = UUID("30000000-0000-0000-0000-000000000001")
COMPLETED_AT = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
PARAMETER = ParameterKey(code_system="usgs", code="00010")
UNIT = CoverageUnit(parameter=PARAMETER, segment_index=0)


class FakeResultReader:
    def __init__(self) -> None:
        self.values: dict[UUID, ResolvedInferenceResult] = {}
        self.calls: list[DependencyOutcome] = []

    async def resolve(self, outcome: DependencyOutcome) -> ResolvedInferenceResult:
        self.calls.append(outcome)
        assert outcome.result_ref is not None
        return self.values[outcome.result_ref.item_id]


def _station_ref(comid: str) -> TopologyStationRef:
    return TopologyStationRef(
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
    )


def _node(
    comid: str,
    *,
    depth: int,
    distance: float,
    target: bool = False,
    inlet: bool = False,
) -> HydrologyNode:
    return HydrologyNode(
        comid=comid,
        point=GeoPoint(latitude=39 + depth / 100, longitude=-77.0),
        depth=depth,
        cumulative_distance_km=distance,
        is_target=target,
        is_inlet=inlet,
        participation=HydrologyParticipation.ACTIVE,
        qualified_units=(UNIT,) if inlet else (),
        station_refs=(_station_ref(comid),) if inlet else (),
    )


def topology(kind: str = "diamond") -> HydrologyGraph:
    if kind == "chain":
        nodes = (
            _node("300", depth=0, distance=0, target=True),
            _node("200", depth=1, distance=1),
            _node("100", depth=2, distance=2, inlet=True),
        )
        edges = (
            HydrologyEdge(
                upstream_comid="100",
                downstream_comid="200",
                distance_km=1,
                participating=True,
            ),
            HydrologyEdge(
                upstream_comid="200",
                downstream_comid="300",
                distance_km=1,
                participating=True,
            ),
        )
        roots = (("100", ("100", "200", "300")),)
        layers = (ComidLayer(("100",)), ComidLayer(("200",)), ComidLayer(("300",)))
    else:
        nodes = (
            _node("300", depth=0, distance=0, target=True),
            _node("100", depth=1, distance=1, inlet=True),
            _node("200", depth=1, distance=1, inlet=True),
        )
        edges = (
            HydrologyEdge(
                upstream_comid="100",
                downstream_comid="300",
                distance_km=1,
                participating=True,
            ),
            HydrologyEdge(
                upstream_comid="200",
                downstream_comid="300",
                distance_km=1,
                participating=True,
            ),
        )
        roots = (
            ("100", ("100", "300")),
            ("200", ("200", "300")),
        )
        layers = (ComidLayer(("100", "200")), ComidLayer(("300",)))
    outcomes = tuple(
        BranchOutcome(
            branch_root_comid=root,
            coverage_unit=UNIT,
            outcome=BranchOutcomeStatus.QUALIFIED,
            stop_comid=root,
            path_to_target=path,
            reason=BranchOutcomeReason.STATION_QUALIFIED,
        )
        for root, path in roots
    )
    return HydrologyGraph(
        target_comid="300",
        coverage=TopologyCoverage.COMPLETE,
        nodes=nodes,
        edges=edges,
        branch_outcomes=outcomes,
        execution_layers=layers,
        limits=TopologyLimits(max_distance_km=100, max_hops=12, max_nodes=128),
        stats=TopologySearchStats(
            discovered_nodes=len(nodes),
            queried_nodes=len(nodes),
            max_depth=max(node.depth for node in nodes),
            max_distance_km=max(node.cumulative_distance_km for node in nodes),
            qualified_inlets=sum(node.is_inlet for node in nodes),
            excluded_nodes=0,
        ),
    )


def inference_result(
    comid: str,
    session_id: UUID,
    *,
    statuses: tuple[OutcomeStatus, ...] = (
        OutcomeStatus.QUANTIFIED,
        OutcomeStatus.DIRECTIONAL,
    ),
    result_id: str | None = None,
    closure_item_id: UUID | None = None,
    time_range: TimeRange | None = None,
    resolution: TimeResolution = TimeResolution.TWELVE_HOURS,
    parameter: WaterQualityParameter | None = None,
) -> InferenceResult:
    requested = time_range or local_day()
    sources = tuple(
        None if status is OutcomeStatus.INDETERMINATE else ResultSource.INFERRED
        for status in statuses
    )
    series = make_series(
        requested,
        resolution=resolution,
        point_statuses=statuses,
        point_sources=sources,
        parameter=parameter,
    )
    points = tuple(
        point.model_copy(
            update={
                "cell_id": (
                    f"cell:{series.parameter.key.code_system}:"
                    f"{series.parameter.key.code}:window-{index:03d}"
                ),
                "unit_id": (
                    series.parameter.canonical_unit
                    if point.outcome_status is OutcomeStatus.QUANTIFIED
                    else None
                ),
            }
        )
        for index, point in enumerate(series.points)
    )
    series = series.model_copy(
        update={
            "points": points,
            "contributing_cell_refs": tuple(point.cell_id for point in points),
        }
    )
    closure_id = closure_item_id or UUID(
        f"40000000-0000-0000-0000-{int(comid):012d}"
    )
    base = make_result(
        time_range=requested,
        resolution=resolution,
        series=(series,),
        analysis_detail=AnalysisDetail(
            summary="Analysis complete",
            steps=(),
            transcript_item_refs=(str(closure_id),),
        ),
    )
    values = base.model_dump(mode="python")
    values.update(
        run_id=RUN_ID,
        session_id=session_id,
        target_comid=comid,
        result_id=result_id or f"result-{comid}",
    )
    return InferenceResult.model_validate(values)


def resolved_result(result: InferenceResult) -> ResolvedInferenceResult:
    return ResolvedInferenceResult(
        result=result,
        closure_refs=tuple(
            ItemReference(item_id=item_id)
            for item_id in result.analysis_detail.transcript_item_refs
        ),
    )


def dependency_outcome(
    comid: str,
    session_id: UUID,
    *,
    status: str = "usable",
    result: InferenceResult | None = None,
    result_item_id: UUID | None = None,
) -> DependencyOutcome:
    item_id = result_item_id or UUID(
        f"50000000-0000-0000-0000-{int(comid):012d}"
    )
    if status in {"usable", "insufficient"}:
        assert result is not None
        result_ref = SessionResultReference(run_id=RUN_ID, item_id=item_id)
        result_summary = WaterbodySessionResultSummary(
            result_id=result.result_id,
            target_comid=comid,
            risk_level=result.overall_assessment.risk_level,
            availability=result.overall_assessment.availability,
            overall_confidence=result.overall_confidence,
        )
    else:
        result_ref = None
        result_summary = None
    usable_refs = (
        tuple(
            StableSourceReference(
                run_id=RUN_ID,
                session_id=session_id,
                item_id=item_id,
                content_type="inference_result",
                content_schema_version=1,
                result_id=result.result_id,
                cell_id=point.cell_id,
                window_id=point.cell_id.rsplit(":", 1)[1],
            )
            for item in result.series
            for point in item.points
            if point.outcome_status is not OutcomeStatus.INDETERMINATE
        )
        if status == "usable" and result is not None
        else ()
    )
    return DependencyOutcome(
        run_id=RUN_ID,
        target_comid=comid,
        session_id=session_id,
        status=status,
        result_ref=result_ref,
        result_summary=result_summary,
        usable_cell_refs=usable_refs,
        discharge_states=(
            (
                DependencyDischargeState(
                    window_id="window-000",
                    outcome=OutcomeStatus.QUANTIFIED,
                ),
            )
            if status == "usable"
            else ()
        ),
        error_ref=(
            SessionResultReference(
                run_id=RUN_ID,
                item_id=UUID("60000000-0000-0000-0000-000000000001"),
            )
            if status == "invalid"
            else None
        ),
        completed_at=COMPLETED_AT,
    )


def store_for(graph: HydrologyGraph, reader: FakeResultReader):
    return RunLocalUpstreamResultStore(
        run_id=RUN_ID,
        topology=graph,
        topology_ref=TranscriptContentRef(
            run_id=RUN_ID,
            item_id=TOPOLOGY_ITEM_ID,
        ),
        result_reader=reader,
    )


def request(
    *,
    agent_comid: str,
    upstreams: tuple[str, ...],
    run_id: UUID = RUN_ID,
    windows: tuple[str, ...] = ("window-000", "window-001"),
) -> UpstreamResultRequest:
    return UpstreamResultRequest(
        operation_id="operation-001",
        run_id=str(run_id),
        session_id=str(DOWNSTREAM_SESSION_ID),
        agent_comid=agent_comid,
        direct_upstream_comids=upstreams,
        parameter_keys=(PARAMETER,),
        affected_window_ids=windows,
    )


def test_publish_is_exact_once_and_rejects_identity_mismatches() -> None:
    async def exercise() -> None:
        reader = FakeResultReader()
        result = inference_result("100", UUID("30000000-0000-0000-0000-000000000100"))
        outcome = dependency_outcome("100", result.session_id, result=result)
        assert outcome.result_ref is not None
        reader.values[outcome.result_ref.item_id] = resolved_result(result)
        store = store_for(topology(), reader)

        await store.publish(outcome)
        with pytest.raises(OutcomeAlreadyPublishedError):
            await store.publish(outcome)
        with pytest.raises(OutcomeIdentityError):
            await store.publish(outcome.model_copy(update={"run_id": OTHER_RUN_ID}))
        with pytest.raises(OutcomeIdentityError):
            await store.publish(outcome.model_copy(update={"target_comid": "999"}))

        assert await store.get("100") == outcome
        assert len(reader.calls) == 1

    asyncio.run(exercise())


def test_chain_authorization_allows_only_participating_direct_upstream() -> None:
    async def exercise() -> None:
        reader = FakeResultReader()
        store = store_for(topology("chain"), reader)
        for comid in ("100", "200"):
            session_id = UUID(f"30000000-0000-0000-0000-{int(comid):012d}")
            result = inference_result(comid, session_id)
            outcome = dependency_outcome(comid, session_id, result=result)
            assert outcome.result_ref is not None
            reader.values[outcome.result_ref.item_id] = resolved_result(result)
            await store.publish(outcome)

        batch = await store.collect(request(agent_comid="300", upstreams=("200",)))

        assert {record.location.comid for record in batch.records} == {"200"}
        with pytest.raises(OutcomeIdentityError):
            await store.collect(request(agent_comid="300", upstreams=("100",)))
        with pytest.raises(OutcomeIdentityError):
            await store.collect(request(agent_comid="100", upstreams=("200",)))
        with pytest.raises(OutcomeIdentityError):
            await store.collect(
                request(
                    agent_comid="300",
                    upstreams=("200",),
                    run_id=OTHER_RUN_ID,
                )
            )

    asyncio.run(exercise())


def test_diamond_manifest_is_stable_complete_and_redacts_invalid_details() -> None:
    async def build(order: tuple[str, ...]):
        reader = FakeResultReader()
        store = store_for(topology(), reader)
        outcomes = {}
        usable_session = UUID("30000000-0000-0000-0000-000000000100")
        usable_result = inference_result("100", usable_session)
        usable = dependency_outcome("100", usable_session, result=usable_result)
        assert usable.result_ref is not None
        reader.values[usable.result_ref.item_id] = resolved_result(usable_result)
        outcomes["100"] = usable
        outcomes["200"] = dependency_outcome(
            "200",
            UUID("30000000-0000-0000-0000-000000000200"),
            status="invalid",
        )
        with pytest.raises(OutcomesNotReadyError):
            await store.build_manifest(
                session_id=DOWNSTREAM_SESSION_ID,
                target_comid="300",
            )
        for comid in order:
            await store.publish(outcomes[comid])
        return await store.build_manifest(
            session_id=DOWNSTREAM_SESSION_ID,
            target_comid="300",
        )

    first = asyncio.run(build(("200", "100")))
    second = asyncio.run(build(("100", "200")))

    assert first == second
    assert tuple(item.upstream_comid for item in first.dependencies) == ("100", "200")
    invalid = first.dependencies[1]
    assert invalid.dependency_status.value == "invalid"
    assert invalid.result_ref is None
    assert invalid.usable_cell_refs == ()
    assert invalid.discharge_states == ()
    assert "60000000-0000-0000-0000-000000000001" not in first.model_dump_json()


@pytest.mark.parametrize("status", ("insufficient", "invalid", "cancelled"))
def test_non_usable_terminal_outcomes_produce_zero_records(status: str) -> None:
    async def exercise() -> None:
        reader = FakeResultReader()
        graph = topology("chain")
        store = store_for(graph, reader)
        upstream_session = UUID("30000000-0000-0000-0000-000000000200")
        result = None
        if status == "insufficient":
            result = inference_result(
                "200",
                upstream_session,
                statuses=(OutcomeStatus.INDETERMINATE, OutcomeStatus.INDETERMINATE),
            )
        outcome = dependency_outcome(
            "200",
            upstream_session,
            status=status,
            result=result,
        )
        if outcome.result_ref is not None:
            assert result is not None
            reader.values[outcome.result_ref.item_id] = resolved_result(result)
        await store.publish(outcome)

        batch = await store.collect(request(agent_comid="300", upstreams=("200",)))
        manifest = await store.build_manifest(
            session_id=DOWNSTREAM_SESSION_ID,
            target_comid="300",
        )

        assert batch.records == ()
        assert batch.completion_proof.complete is True
        assert manifest.dependencies[0].dependency_status.value == status
        assert manifest.dependencies[0].result_ref is None
        assert manifest.dependencies[0].usable_cell_refs == ()

    asyncio.run(exercise())
