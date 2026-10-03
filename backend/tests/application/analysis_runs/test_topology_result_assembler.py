import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    SessionSummary,
)
from app.application.analysis_runs.topology_result_assembler import (
    TargetWaterbodyFailedError,
    TopologyResultAssembler,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    GraphCoverage,
)
from app.domain import OutcomeStatus, ParameterKey, WaterQualityParameter
from tests.application.analysis_runs._topology_scheduler_support import (
    outcome,
    topology,
)
from tests.application.evidence_tools.test_upstream_result_store import (
    FakeResultReader,
    dependency_outcome,
    inference_result,
    resolved_result,
)


RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 10, 3, 14, tzinfo=timezone.utc)


def committed_session(comid, result, *, status=AgentSessionStatus.COMPLETED):
    session_id = result.session_id
    dependency_status = (
        "usable"
        if any(
            point.outcome_status is not OutcomeStatus.INDETERMINATE
            for series in result.series
            for point in series.points
        )
        else "insufficient"
    )
    projected = dependency_outcome(
        comid,
        session_id,
        status=dependency_status,
        result=result,
    )
    return SessionSummary(
        session_id=session_id,
        agent_role=AgentRole.WATERBODY,
        target_comid=comid,
        status=status,
        created_at=NOW,
        updated_at=NOW,
        result_ref=projected.result_ref if status is AgentSessionStatus.COMPLETED else None,
        result_summary=(
            projected.result_summary
            if status is AgentSessionStatus.COMPLETED
            else None
        ),
    )


def test_committed_result_is_the_only_source_of_usable_outcome() -> None:
    async def exercise() -> None:
        session_id = UUID("30000000-0000-0000-0000-000000000100")
        result = inference_result("100", session_id)
        session = committed_session("100", result)
        reader = FakeResultReader()
        reader.values[session.result_ref.item_id] = resolved_result(result)

        assembled = await TopologyResultAssembler(reader).dependency_outcome(
            RUN_ID,
            session,
        )

        assert assembled.status is DependencyStatus.USABLE
        assert assembled.result_ref == session.result_ref
        assert tuple(ref.cell_id for ref in assembled.usable_cell_refs) == tuple(
            point.cell_id for series in result.series for point in series.points
        )
        assert all(
            ref.session_id == session.session_id
            for ref in assembled.usable_cell_refs
        )

    asyncio.run(exercise())


def test_committed_discharge_result_preserves_each_window_state() -> None:
    async def exercise() -> None:
        session_id = UUID("30000000-0000-0000-0000-000000000101")
        result = inference_result(
            "101",
            session_id,
            parameter=WaterQualityParameter(
                key=ParameterKey(code_system="usgs", code="00060"),
                name="Discharge",
                canonical_unit="ft3/s",
                category="hydrology",
            ),
        )
        session = committed_session("101", result)
        reader = FakeResultReader()
        reader.values[session.result_ref.item_id] = resolved_result(result)

        assembled = await TopologyResultAssembler(reader).dependency_outcome(
            RUN_ID,
            session,
        )

        assert tuple(
            (state.window_id, state.outcome)
            for state in assembled.discharge_states
        ) == (
            ("window-000", OutcomeStatus.QUANTIFIED),
            ("window-001", OutcomeStatus.DIRECTIONAL),
        )

    asyncio.run(exercise())


def test_indeterminate_result_and_failed_session_map_to_canonical_values() -> None:
    async def exercise() -> None:
        insufficient_id = UUID("30000000-0000-0000-0000-000000000200")
        result = inference_result(
            "200",
            insufficient_id,
            statuses=(OutcomeStatus.INDETERMINATE,) * 2,
        )
        session = committed_session("200", result)
        reader = FakeResultReader()
        reader.values[session.result_ref.item_id] = resolved_result(result)
        assembler = TopologyResultAssembler(reader)

        insufficient = await assembler.dependency_outcome(RUN_ID, session)
        failed = await assembler.dependency_outcome(
            RUN_ID,
            SessionSummary(
                session_id=UUID("30000000-0000-0000-0000-000000000300"),
                agent_role=AgentRole.WATERBODY,
                target_comid="300",
                status=AgentSessionStatus.FAILED,
                created_at=NOW,
                updated_at=NOW,
            ),
        )

        assert insufficient.status is DependencyStatus.INSUFFICIENT
        assert insufficient.usable_cell_refs == ()
        assert failed.status is DependencyStatus.INVALID
        assert failed.result_ref is None

    asyncio.run(exercise())


def test_aggregation_preserves_order_partial_and_target_business_result() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    assembler = TopologyResultAssembler(FakeResultReader())

    aggregated = assembler.aggregate(
        graph,
        (
            outcome("300", DependencyStatus.INSUFFICIENT),
            outcome("200", DependencyStatus.INVALID),
            outcome("100", DependencyStatus.USABLE),
        ),
    )

    assert tuple(item.target_comid for item in aggregated.outcomes) == (
        "100",
        "200",
        "300",
    )
    assert aggregated.target_outcome.status is DependencyStatus.INSUFFICIENT
    assert aggregated.graph_coverage is GraphCoverage.PARTIAL


def test_target_technical_failure_is_fatal() -> None:
    graph = topology(("100", "300"))
    assembler = TopologyResultAssembler(FakeResultReader())

    with pytest.raises(TargetWaterbodyFailedError):
        assembler.aggregate(
            graph,
            (
                outcome("100", DependencyStatus.USABLE),
                outcome("300", DependencyStatus.INVALID),
            ),
        )
