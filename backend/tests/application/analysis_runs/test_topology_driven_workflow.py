import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisConfigurationSnapshot,
    AnalysisPhase,
    AnalysisRunSnapshot,
    PublicError,
    RunStatus,
    SessionSummary,
)
from app.application.analysis_runs.query_topology_workflow import (
    QueryTopologyWorkflow,
)
from app.application.analysis_runs.topology_driven_workflow import (
    ProductionTopologyNodeRunner,
    TopologyWaterbodyExecutionStage,
)
from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    GraphCoverage,
)
from app.application.analysis_runs.topology_scheduler import (
    TopologyScheduleState,
    WaterbodyWorkflowLimiter,
    build_topology_execution_view,
)
from app.application.analysis_runs.workflow import (
    CancellationToken,
    WorkflowContext,
    WorkflowStopped,
)
from app.application.analysis_runs.zero_model_result import (
    DeterministicInsufficientResultAssembler,
)
from app.application.evidence_tools import (
    RunLocalUpstreamResultRegistry,
    RunLocalUpstreamResultStore,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    EvidenceCellRole,
    OutcomeStatus,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
)
from app.infrastructure.config import (
    AppSettings,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.analysis_runs._topology_scheduler_support import topology
from tests.application.evidence_tools.test_upstream_result_store import (
    FakeResultReader,
    inference_result,
    resolved_result,
)
from tests.m4_support import (
    MODEL_CATALOG,
    analysis_context,
    create_request_bytes,
    create_test_app,
)


RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
TARGET = "12345678"


class SavedQueryStage:
    def __init__(self, value) -> None:
        self.value = value

    async def understand(self, context):
        session = await context.operations.prepare_agent_session(
            context.run.run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await context.operations.mark_agent_session_running(
            context.run.run_id,
            session.session_id,
        )
        item = await context.operations.reserve_transcript_item(
            context.run.run_id,
            session_id=session.session_id,
        )
        await context.operations.finalize_transcript_item(
            context.run.run_id,
            item.item_id,
            "query_understood",
            1,
            self.value,
        )
        await context.operations.complete_agent_session(
            context.run.run_id,
            session.session_id,
        )
        return self.value


class FixedTopologyStage:
    def __init__(self, graph) -> None:
        self.graph = graph

    async def build(self, context):
        del context
        return self.graph


class RecordingSummaryBoundary:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.aggregation = None

    async def wait(self, context, aggregation) -> None:
        self.aggregation = aggregation
        self.reached.set()
        await context.cancellation.wait()
        raise WorkflowStopped()


class WorkflowNodeRunnerFactory:
    def __init__(self, operations, reader, statuses, releases=None) -> None:
        self.operations = operations
        self.reader = reader
        self.statuses = statuses
        self.releases = releases or {}
        self.requests = []
        self.model_calls = []
        self.store = None
        self.assembler = TopologyResultAssembler(reader)

    def build(self, *, outcome_store, **kwargs):
        del kwargs
        self.store = outcome_store
        return self

    async def run(self, request):
        self.requests.append(request)
        session = await self.operations.prepare_agent_session(
            request.run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid=request.node.comid,
        )
        await self.operations.mark_agent_session_running(
            request.run_id,
            session.session_id,
        )
        release = self.releases.get(request.node.comid)
        if release is not None:
            await release.wait()
        status = self.statuses.get(
            request.node.comid,
            DependencyStatus.INSUFFICIENT,
        )
        if status is DependencyStatus.INVALID:
            terminal = await self.operations.fail_agent_session(
                request.run_id,
                session.session_id,
                PublicError(
                    error_id=uuid4(),
                    code="waterbody_model_response_invalid",
                    message="The waterbody model returned an invalid response.",
                    failed_stage="waterbody_execution",
                    retryable=False,
                ),
            )
            self.model_calls.append(request.node.comid)
        else:
            point_status = (
                OutcomeStatus.QUANTIFIED
                if status is DependencyStatus.USABLE
                else OutcomeStatus.INDETERMINATE
            )
            result = inference_result(
                request.node.comid,
                session.session_id,
                statuses=(point_status,) * 2,
            )
            terminal = await self.operations.complete_waterbody_agent_session(
                request.run_id,
                session.session_id,
                result,
            )
            self.reader.values[terminal.result_ref.item_id] = resolved_result(result)
            if status is DependencyStatus.USABLE:
                self.model_calls.append(request.node.comid)
        return await self.assembler.dependency_outcome(request.run_id, terminal)

    async def settle_failure(self, request, error):
        raise AssertionError(f"unexpected fake node failure for {request.node.comid}") from error


class NodeRunnerOperations:
    def __init__(self) -> None:
        self.sessions = {}

    async def prepare_agent_session(self, run_id, *, agent_role, target_comid=None):
        session = SessionSummary(
            session_id=uuid4(),
            agent_role=agent_role,
            target_comid=target_comid,
            status=AgentSessionStatus.PENDING,
            created_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
            updated_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
        )
        self.sessions[session.session_id] = session
        return session

    async def mark_agent_session_running(self, run_id, session_id):
        del run_id
        return self._transition(session_id, AgentSessionStatus.RUNNING)

    async def get_session(self, run_id, session_id):
        del run_id
        return self.sessions[session_id]

    async def fail_agent_session(self, run_id, session_id, error):
        del run_id, error
        return self._transition(session_id, AgentSessionStatus.FAILED)

    async def cancel_agent_session(self, run_id, session_id):
        del run_id
        return self._transition(session_id, AgentSessionStatus.CANCELLED)

    def _transition(self, session_id, status):
        current = self.sessions[session_id]
        updated = SessionSummary(
            session_id=current.session_id,
            agent_role=current.agent_role,
            target_comid=current.target_comid,
            status=status,
            created_at=current.created_at,
            updated_at=current.updated_at + timedelta(seconds=1),
        )
        self.sessions[session_id] = updated
        return updated


class NodeRunnerSessionReader:
    def __init__(self, operations) -> None:
        self.operations = operations

    async def read_agent_session(self, scope, session_id):
        del scope
        return self.operations.sessions[UUID(session_id)]


class RunnableAcquisition:
    def __init__(self) -> None:
        self.scopes = []

    async def start(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(
            run_id=scope.run_id,
            session_id=scope.session_id,
            cells=(
                SimpleNamespace(
                    role=EvidenceCellRole.TARGET,
                    direct_fragment_refs=("evidence:local",),
                ),
            ),
            usable_upstream_comids=(),
            block_facts=(),
            stop_before_model=False,
            has_valid_bundle_binding=lambda: True,
        )


class AuthoritativeRaceRunner:
    def __init__(self, operations, committed_status, cancellation_event) -> None:
        self.operations = operations
        self.committed_status = committed_status
        self.cancellation_event = cancellation_event
        self.calls = []

    def session_deadline(self):
        return datetime(2026, 10, 3, 15, tzinfo=timezone.utc)

    async def run(self, task, **kwargs):
        del kwargs
        self.calls.append(task)
        if self.committed_status is AgentSessionStatus.CANCELLED:
            await self.operations.cancel_agent_session(
                task.task.run_id,
                task.task.session_id,
            )
        else:
            await self.operations.fail_agent_session(
                task.task.run_id,
                task.task.session_id,
                PublicError(
                    error_id=uuid4(),
                    code="persistence_failed",
                    message="The committed session failed.",
                    failed_stage="waterbody_execution",
                    retryable=False,
                ),
            )
        if self.cancellation_event is not None:
            self.cancellation_event.set()
        return SimpleNamespace(status="completed", result="late-uncommitted")


@asynccontextmanager
async def workflow_runtime(paths, statuses, *, releases=None):
    graph = topology(*paths)
    context = analysis_context().model_copy(
        update={
            "target": analysis_context().target.model_copy(
                update={"comid": graph.target_comid}
            )
        }
    )
    workflow = QueryTopologyWorkflow(
        SavedQueryStage(context),
        FixedTopologyStage(graph),
    )
    settings = AppSettings(
        _env_file=None,
        analysis_timezone=context.analysis_timezone,
        live_state_grace_seconds=60,
    )
    application = create_test_app(
        settings=settings,
        workflow=workflow,
        transcript_store=InMemoryTranscriptStore(),
        id_factory=lambda: RUN_ID,
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        reader = FakeResultReader()
        assembler = TopologyResultAssembler(reader)
        stores = RunLocalUpstreamResultRegistry(reader)
        node_factory = WorkflowNodeRunnerFactory(
            coordinator,
            reader,
            statuses,
            releases,
        )
        boundary = RecordingSummaryBoundary()
        workflow.bind_execution_stage(
            TopologyWaterbodyExecutionStage(
                operations=coordinator,
                outcome_stores=stores,
                node_runner_factory=node_factory,
                result_assembler=assembler,
                limiter=WaterbodyWorkflowLimiter(4),
                summary_boundary=boundary,
            )
        )
        yield coordinator, node_factory, boundary


async def create_and_wait_for_boundary(coordinator, boundary):
    created = await coordinator.create_run(create_request_bytes())
    await asyncio.wait_for(boundary.reached.wait(), timeout=2)
    return created


async def cancel_and_wait(coordinator, run_id):
    await coordinator.cancel_run(run_id)
    await asyncio.wait_for(
        coordinator._runs[run_id].workflow_quiesced.wait(),
        timeout=2,
    )


@pytest.mark.parametrize(
    "paths",
    (
        (("100", "200", TARGET),),
        (("100", "300", TARGET), ("200", "300", TARGET)),
    ),
)
def test_production_workflow_executes_each_chain_and_diamond_node_once(paths) -> None:
    async def exercise() -> None:
        statuses = {
            comid: DependencyStatus.USABLE
            for path in paths
            for comid in path
        }
        async with workflow_runtime(paths, statuses) as (
            coordinator,
            node_factory,
            boundary,
        ):
            created = await create_and_wait_for_boundary(coordinator, boundary)
            snapshot = await coordinator.get_snapshot(created.run_id)
            sessions = await coordinator.list_sessions(created.run_id)
            waterbodies = tuple(
                session for session in sessions
                if session.agent_role is AgentRole.WATERBODY
            )

            assert snapshot.status is RunStatus.RUNNING
            assert coordinator._runs[created.run_id].analysis_phase is (
                AnalysisPhase.RUN_SUMMARIZATION
            )
            assert len(waterbodies) == len(statuses)
            assert all(
                session.status is AgentSessionStatus.COMPLETED
                for session in waterbodies
            )
            assert len(node_factory.requests) == len(statuses)
            assert len({item.node.comid for item in node_factory.requests}) == len(
                statuses
            )
            assert set(node_factory.model_calls) == set(statuses)
            assert boundary.aggregation.graph_coverage is GraphCoverage.COMPLETE
            for comid in statuses:
                assert await node_factory.store.get(comid) is not None

            await cancel_and_wait(coordinator, created.run_id)

    asyncio.run(exercise())


def test_diamond_creates_downstream_session_only_after_both_sources_settle() -> None:
    async def exercise() -> None:
        releases = {"100": asyncio.Event(), "200": asyncio.Event()}
        paths = (("100", "300", TARGET), ("200", "300", TARGET))
        statuses = {
            comid: DependencyStatus.USABLE
            for path in paths
            for comid in path
        }
        async with workflow_runtime(paths, statuses, releases=releases) as (
            coordinator,
            _,
            boundary,
        ):
            created = await coordinator.create_run(create_request_bytes())
            while len(await coordinator.list_sessions(created.run_id)) < 3:
                await asyncio.sleep(0)
            sessions = await coordinator.list_sessions(created.run_id)
            assert {item.target_comid for item in sessions if item.target_comid} == {
                "100",
                "200",
            }

            releases["100"].set()
            await asyncio.sleep(0.02)
            assert "300" not in {
                item.target_comid
                for item in await coordinator.list_sessions(created.run_id)
                if item.target_comid
            }
            releases["200"].set()
            await asyncio.wait_for(boundary.reached.wait(), timeout=2)
            assert {item.target_comid for item in await coordinator.list_sessions(
                created.run_id
            ) if item.target_comid} == {"100", "200", "300", TARGET}

            await cancel_and_wait(coordinator, created.run_id)

    asyncio.run(exercise())


def test_partial_and_all_insufficient_reach_summary_boundary() -> None:
    async def exercise(statuses, expected_coverage, expected_model_calls) -> None:
        paths = (("100", TARGET), ("200", TARGET))
        async with workflow_runtime(paths, statuses) as (
            coordinator,
            node_factory,
            boundary,
        ):
            created = await create_and_wait_for_boundary(coordinator, boundary)

            target_request = next(
                item for item in node_factory.requests
                if item.node.comid == TARGET
            )
            assert len(target_request.dependency_outcomes) == 2
            assert boundary.aggregation.graph_coverage is expected_coverage
            assert set(node_factory.model_calls) == expected_model_calls
            await cancel_and_wait(coordinator, created.run_id)

    asyncio.run(exercise(
        {
            "100": DependencyStatus.USABLE,
            "200": DependencyStatus.INVALID,
            TARGET: DependencyStatus.INSUFFICIENT,
        },
        GraphCoverage.PARTIAL,
        {"100", "200"},
    ))
    asyncio.run(exercise(
        {
            "100": DependencyStatus.INSUFFICIENT,
            "200": DependencyStatus.INSUFFICIENT,
            TARGET: DependencyStatus.INSUFFICIENT,
        },
        GraphCoverage.COMPLETE,
        set(),
    ))


def test_target_failure_is_fatal_but_target_indeterminate_is_not() -> None:
    async def target_failed() -> None:
        paths = (("100", TARGET),)
        async with workflow_runtime(
            paths,
            {
                "100": DependencyStatus.USABLE,
                TARGET: DependencyStatus.INVALID,
            },
        ) as (coordinator, _, boundary):
            created = await coordinator.create_run(create_request_bytes())
            await asyncio.wait_for(
                coordinator._runs[created.run_id].workflow_quiesced.wait(),
                timeout=2,
            )
            snapshot = await coordinator.get_snapshot(created.run_id)
            assert snapshot.status is RunStatus.FAILED
            assert boundary.reached.is_set() is False
            assert coordinator._runs[created.run_id].last_error.code == (
                "target_waterbody_failed"
            )

    async def target_indeterminate() -> None:
        paths = (("100", TARGET),)
        async with workflow_runtime(
            paths,
            {
                "100": DependencyStatus.INSUFFICIENT,
                TARGET: DependencyStatus.INSUFFICIENT,
            },
        ) as (coordinator, _, boundary):
            created = await create_and_wait_for_boundary(coordinator, boundary)
            assert (await coordinator.get_snapshot(created.run_id)).status is (
                RunStatus.RUNNING
            )
            assert boundary.aggregation.target_outcome.status is (
                DependencyStatus.INSUFFICIENT
            )
            await cancel_and_wait(coordinator, created.run_id)

    asyncio.run(target_failed())
    asyncio.run(target_indeterminate())


def test_node_runner_uses_committed_session_and_suppresses_cancel_winner() -> None:
    async def exercise(committed_status, *, cancel_wins=False):
        graph = topology(("100", TARGET))
        base_context = analysis_context()
        saved_context = base_context.model_copy(
            update={
                "target": base_context.target.model_copy(
                    update={"comid": TARGET}
                ),
                "params_used": (
                    base_context.params_used[0].model_copy(
                        update={
                            "canonical_name": "Dissolved oxygen",
                            "canonical_unit": "mg/l",
                        }
                    ),
                ),
                "supporting_params": (
                    base_context.supporting_params[0].model_copy(
                        update={"canonical_name": "Discharge"}
                    ),
                ),
            }
        )
        stop_requested = asyncio.Event()
        cancellation = CancellationToken(stop_requested)
        run = AnalysisRunSnapshot(
            run_id=RUN_ID,
            status=RunStatus.RUNNING,
            created_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
            updated_at=datetime(2026, 10, 3, 14, tzinfo=timezone.utc),
            configuration=AnalysisConfigurationSnapshot(
                analysis_timezone=saved_context.analysis_timezone,
                time_resolution_preference=TimeResolutionPreference.TWELVE_HOURS,
                time_resolution_preference_source=(
                    TimeResolutionPreferenceSource.DEFAULT
                ),
                parser_model=saved_context.parser_model,
                waterbody_model=saved_context.waterbody_model,
                memory_mode=saved_context.memory_mode,
            ),
        )
        operations = NodeRunnerOperations()
        result_reader = FakeResultReader()
        result_assembler = TopologyResultAssembler(result_reader)
        topology_ref = TranscriptContentRef(run_id=RUN_ID, item_id=uuid4())
        store = RunLocalUpstreamResultStore(
            run_id=RUN_ID,
            topology=graph,
            topology_ref=topology_ref,
            result_reader=result_reader,
        )
        waterbody_runner = AuthoritativeRaceRunner(
            operations,
            committed_status,
            stop_requested if cancel_wins else None,
        )
        runner = ProductionTopologyNodeRunner(
            context=WorkflowContext(
                run=run,
                query=saved_context.query,
                cancellation=cancellation,
                request=SimpleNamespace(),
                operations=operations,
            ),
            analysis_context=saved_context,
            topology=graph,
            analysis_context_ref=TranscriptContentRef(
                run_id=RUN_ID,
                item_id=uuid4(),
            ),
            topology_ref=topology_ref,
            outcome_store=store,
            operations=operations,
            acquisition=RunnableAcquisition(),
            session_reader=NodeRunnerSessionReader(operations),
            waterbody_runner=waterbody_runner,
            result_assembler=result_assembler,
            capability_registry=load_parameter_capability_registry(),
            model_catalog=MODEL_CATALOG.snapshot(),
            zero_model=DeterministicInsufficientResultAssembler(
                capability_registry=load_parameter_capability_registry(),
                result_id_factory=lambda: "unused-zero-model-result",
                clock=SimpleNamespace(
                    now=lambda: datetime(2026, 10, 3, 14, tzinfo=timezone.utc)
                ),
            ),
            error_id_factory=uuid4,
        )
        state = TopologyScheduleState(
            run_id=RUN_ID,
            view=build_topology_execution_view(graph),
        )
        request = await state.claim_ready()
        assert request is not None and request.node.comid == "100"
        if cancel_wins:
            with pytest.raises(asyncio.CancelledError):
                await runner.run(request)
            assert len(waterbody_runner.calls) == 1
            return None
        outcome = await runner.run(request)
        assert len(waterbody_runner.calls) == 1
        task = waterbody_runner.calls[0]
        assert task.context is saved_context
        assert task.task.waterbody_model == saved_context.waterbody_model
        assert task.task.memory_mode is saved_context.memory_mode
        assert task.task.evidence_scope.mode is saved_context.mode
        assert task.task.evidence_scope.analysis_timezone == (
            saved_context.analysis_timezone
        )
        assert task.task.evidence_scope.requested_parameters == tuple(
            item.key for item in saved_context.params_used
        )
        assert tuple(
            window.segment_index
            for window in task.task.evidence_scope.time_windows
        ) == (0,) * len(task.task.evidence_scope.time_windows)
        return outcome

    failed = asyncio.run(exercise(AgentSessionStatus.FAILED))
    cancelled = asyncio.run(exercise(AgentSessionStatus.CANCELLED))
    suppressed = asyncio.run(exercise(
        AgentSessionStatus.CANCELLED,
        cancel_wins=True,
    ))

    assert failed.status is DependencyStatus.INVALID
    assert cancelled.status is DependencyStatus.CANCELLED
    assert suppressed is None
