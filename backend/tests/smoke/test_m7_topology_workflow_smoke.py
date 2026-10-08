import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    PublicError,
    RunStatus,
)
from app.application.analysis_runs.query_topology_workflow import QueryTopologyWorkflow
from app.application.analysis_runs.topology_driven_workflow import (
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
    TopologyScheduler,
    WaterbodyWorkflowLimiter,
)
from app.application.evidence_tools import RunLocalUpstreamResultRegistry
from app.application.reasoning.batch_planner import (
    ModelFitCapability,
    ModelSizeEstimate,
)
from app.application.reasoning import WaterbodyAgentRunStatus
from app.application.reasoning.model_models import (
    Terminal, WaterbodyFinishReason, WaterbodyModelPhase,
)
from app.application.run_summary.input_builder import RunSummaryInputBuilder
from app.application.run_summary.models import RunSummaryAgentMessage, RunSummaryResult
from app.application.run_summary.runner import (
    RunSummaryRunOutcome,
    RunSummaryRunStatus,
)
from app.application.run_summary.stage import RunSummaryStage, RunSummaryStageOutcome
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    TranscriptContentRef,
)
from app.domain import OutcomeStatus
from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime.m7 import (
    CompleteRunSummaryBoundary,
    ProductionRunSummaryRunnerFactory,
)
from app.ports import WaterbodyModelCancelledError
from tests.application.analysis_runs._topology_scheduler_support import (
    RUN_ID,
    ConcurrencyProbe,
    RecordingOutcomePort,
    RecordingRunner,
    large_topology,
    topology,
)
from tests.application.analysis_runs.test_topology_driven_workflow import (
    FixedTopologyStage,
    SavedQueryStage,
    WorkflowNodeRunnerFactory,
)
from tests.application.evidence_tools.test_upstream_result_store import (
    FakeResultReader,
    dependency_outcome,
    inference_result,
    resolved_result,
)
from tests.application.evidence_tools.fakes import FixedClock
from tests.application.reasoning.test_planning_loop import _fixture
from tests.application.reasoning.test_runner import (
    AdaptiveWaterbodyModelPort,
    _runner,
)
from tests.m4_support import (
    WATERBODY_MODEL,
    analysis_context,
    create_request_bytes,
    create_test_app,
)


TARGET = "12345678"
NOW = datetime(2026, 10, 4, 1, tzinfo=UTC)


class RecordingModelFitRouter:
    def __init__(self) -> None:
        self.models = []

    def model_fit(self, model):
        self.models.append(model)
        return ModelFitCapability(
            model=model,
            context_window_tokens=1_000_000,
            max_output_tokens=4_096,
        )


class ProviderCancelledSummaryRouter(RecordingModelFitRouter):
    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    def estimate(self, model, *, serialized_request, serialized_response_shape):
        assert model == WATERBODY_MODEL
        assert serialized_request and serialized_response_shape
        return ModelSizeEstimate(
            input_tokens=100,
            output_tokens=100,
            estimated_output_bytes=1_000,
        )

    async def stream(self, request):
        self.requests.append(request)
        yield Terminal(
            operation_id=request.operation_id,
            model=request.model,
            provider_response_id="response-cancelled",
            finish_reason=WaterbodyFinishReason.CANCELLED,
        )


class ProviderCancelledBranchFactory(WorkflowNodeRunnerFactory):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.provider_outcome = None

    async def run(self, request):
        if request.node.comid != "100":
            return await super().run(request)
        port = AdaptiveWaterbodyModelPort(
            fail_phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
            failure_type=WaterbodyModelCancelledError,
        )
        _, _, loop, task = await _fixture(port, water_mode="full")
        self.provider_outcome = await _runner(loop, port).run(
            task, deadline=FixedClock().now() + timedelta(minutes=5)
        )
        assert self.provider_outcome.status is WaterbodyAgentRunStatus.FAILED
        self.requests.append(request)
        session = await self.operations.prepare_agent_session(
            request.run_id, agent_role=AgentRole.WATERBODY,
            target_comid=request.node.comid,
        )
        manifest = await self.store.build_manifest(
            session_id=session.session_id, target_comid=request.node.comid,
        )
        manifest_item = await self.operations.reserve_transcript_item(
            request.run_id, session_id=session.session_id,
        )
        await self.operations.finalize_transcript_item(
            request.run_id, manifest_item.item_id,
            "waterbody_dependency_manifest", 1, manifest,
        )
        self.manifests.append(manifest)
        await self.operations.mark_agent_session_running(
            request.run_id, session.session_id,
        )
        terminal = await self.operations.fail_agent_session(
            request.run_id, session.session_id, self.provider_outcome.error,
        )
        return await self.assembler.dependency_outcome(request.run_id, terminal)

class FixtureSummaryStage:
    def __init__(self, operations, *, fail: bool = False) -> None:
        self.operations = operations
        self.fail = fail
        self.calls = []
        self.model_calls = 0
        self.tool_calls = 0
        self.waterbodies_terminal_before_create = False

    async def run(self, **arguments):
        self.calls.append(arguments)
        sessions = await self.operations.list_sessions(arguments["run_id"])
        waterbodies = [
            session
            for session in sessions
            if session.agent_role is AgentRole.WATERBODY
        ]
        self.waterbodies_terminal_before_create = bool(waterbodies) and all(
            session.status
            in {
                AgentSessionStatus.COMPLETED,
                AgentSessionStatus.FAILED,
                AgentSessionStatus.CANCELLED,
            }
            for session in waterbodies
        )
        session = await self.operations.prepare_agent_session(
            arguments["run_id"],
            agent_role=AgentRole.RUN_SUMMARIZER,
        )
        await self.operations.mark_agent_session_running(
            arguments["run_id"],
            session.session_id,
        )
        self.model_calls += 1
        if self.fail:
            error = PublicError(
                error_id=uuid4(),
                code="run_summary_failed",
                message="The plain-language summary could not be generated.",
                failed_stage="run_summarization",
                retryable=True,
            )
            await self.operations.fail_agent_session(
                arguments["run_id"],
                session.session_id,
                error,
            )
            outcome = RunSummaryRunOutcome(
                status=RunSummaryRunStatus.FAILED,
                result=None,
                error=error,
                model_calls=1,
                repaired=False,
            )
        else:
            result = RunSummaryResult(
                run_id=arguments["run_id"],
                session_id=session.session_id,
                input_digest="sha256:m7-fixed-smoke",
                model=arguments["model"],
                language="en",
                plain_language_summary="The target result is available.",
                generated_at=NOW,
            )
            await self.operations.complete_run_summary_agent_session(
                arguments["run_id"],
                session.session_id,
                result,
                message=RunSummaryAgentMessage(
                    run_id=arguments["run_id"],
                    session_id=session.session_id,
                    message_id="validated-summary",
                    operation_id=uuid5(
                        NAMESPACE_URL,
                        f"summary-message:{session.session_id}",
                    ),
                    text=result.plain_language_summary,
                    generation_status=GenerationStatus.COMPLETED,
                    end_reason=MessageEndReason.COMPLETED,
                ),
            )
            outcome = RunSummaryRunOutcome(
                status=RunSummaryRunStatus.COMPLETED,
                result=result,
                error=None,
                model_calls=1,
                repaired=False,
            )
        return RunSummaryStageOutcome(
            session_id=session.session_id,
            outcome=outcome,
        )


@dataclass(slots=True)
class FixtureRuntime:
    application: object
    coordinator: object
    node_factory: WorkflowNodeRunnerFactory
    summary_stage: object
    model_router: RecordingModelFitRouter


@asynccontextmanager
async def complete_workflow_runtime(
    paths,
    statuses,
    *,
    summary_failed=False,
    summary_provider_cancelled=False,
    settings=None,
    transcript_store=None,
    node_factory_class=WorkflowNodeRunnerFactory,
):
    graph = topology(*paths)
    base_context = analysis_context("M7 fixed topology smoke")
    saved_context = base_context.model_copy(
        update={
            "target": base_context.target.model_copy(
                update={"comid": graph.target_comid}
            )
        }
    )
    workflow = QueryTopologyWorkflow(
        SavedQueryStage(saved_context),
        FixedTopologyStage(graph),
    )
    resolved_settings = settings or AppSettings(
        _env_file=None,
        analysis_timezone=saved_context.analysis_timezone,
        live_state_grace_seconds=60,
    )
    resolved_settings = resolved_settings.model_copy(
        update={
            "analysis_timezone": saved_context.analysis_timezone,
            "live_state_grace_seconds": 60,
        }
    )
    application = create_test_app(
        settings=resolved_settings,
        workflow=workflow,
        transcript_store=transcript_store or InMemoryTranscriptStore(),
        id_factory=lambda: RUN_ID,
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        reader = FakeResultReader()
        assembler = TopologyResultAssembler(reader)
        node_factory = node_factory_class(
            coordinator,
            reader,
            statuses,
            result_model=(
                WATERBODY_MODEL if summary_provider_cancelled else None
            ),
        )
        if summary_provider_cancelled:
            model_router = ProviderCancelledSummaryRouter()
            m6_runtime = SimpleNamespace(
                model_port_router=model_router,
                private_values=(),
            )
            clock = SimpleNamespace(now=lambda: NOW)
            summary_stage = RunSummaryStage(
                operations=coordinator,
                input_builder=RunSummaryInputBuilder(
                    result_reader=reader,
                    size_estimator=model_router,
                ),
                runner_factory=ProductionRunSummaryRunnerFactory(
                    operations=coordinator,
                    m6_runtime=m6_runtime,
                    clock=clock,
                    operation_id_factory=uuid4,
                    error_id_factory=uuid4,
                ),
                error_id_factory=uuid4,
            )
        else:
            summary_stage = FixtureSummaryStage(
                coordinator,
                fail=summary_failed,
            )
            model_router = RecordingModelFitRouter()
            m6_runtime = SimpleNamespace(model_port_router=model_router)
            clock = SimpleNamespace(now=lambda: NOW)
        boundary = CompleteRunSummaryBoundary(
            operations=coordinator,
            summary_stage=summary_stage,
            result_assembler=assembler,
            m6_runtime=m6_runtime,
            clock=clock,
            timeout_seconds=120,
        )
        workflow.bind_execution_stage(
            TopologyWaterbodyExecutionStage(
                operations=coordinator,
                outcome_stores=RunLocalUpstreamResultRegistry(reader),
                node_runner_factory=node_factory,
                result_assembler=assembler,
                limiter=WaterbodyWorkflowLimiter(4),
                summary_boundary=boundary,
            )
        )
        yield FixtureRuntime(
            application=application,
            coordinator=coordinator,
            node_factory=node_factory,
            summary_stage=summary_stage,
            model_router=model_router,
        )


async def run_to_quiescence(runtime):
    created = await runtime.coordinator.create_run(
        create_request_bytes("M7 fixed topology smoke")
    )
    await asyncio.wait_for(
        runtime.coordinator._runs[created.run_id].workflow_quiesced.wait(),
        timeout=5,
    )
    stream = await runtime.coordinator.subscribe_run(created.run_id)
    snapshot = await anext(stream)
    await stream.aclose()
    return created, snapshot


@pytest.mark.parametrize(
    "paths,statuses,coverage,model_calls,direct_statuses",
    (
        (
            (("100", "200", TARGET),),
            {"100": "usable", "200": "usable", TARGET: "usable"},
            GraphCoverage.COMPLETE,
            {"100", "200", TARGET},
            {DependencyStatus.USABLE},
        ),
        (
            (("100", "300", TARGET), ("200", "300", TARGET)),
            {comid: "usable" for comid in ("100", "200", "300", TARGET)},
            GraphCoverage.COMPLETE,
            {"100", "200", "300", TARGET},
            {DependencyStatus.USABLE},
        ),
        (
            (("100", TARGET), ("200", TARGET)),
            {comid: "insufficient" for comid in ("100", "200", TARGET)},
            GraphCoverage.COMPLETE,
            set(),
            {DependencyStatus.INSUFFICIENT},
        ),
        (
            (("100", TARGET), ("200", TARGET)),
            {"100": "invalid", "200": "usable", TARGET: "insufficient"},
            GraphCoverage.PARTIAL,
            {"100", "200"},
            {DependencyStatus.INVALID, DependencyStatus.USABLE},
        ),
    ),
    ids=("three-node-chain", "diamond", "all-insufficient", "partial"),
)
def test_fixed_profiles_complete_once_with_summary_refs(
    paths,
    statuses,
    coverage,
    model_calls,
    direct_statuses,
) -> None:
    async def exercise() -> None:
        typed_statuses = {
            comid: DependencyStatus(status) for comid, status in statuses.items()
        }
        async with complete_workflow_runtime(paths, typed_statuses) as runtime:
            created, snapshot = await run_to_quiescence(runtime)
            sessions = await runtime.coordinator.list_sessions(created.run_id)
            waterbodies = [
                session
                for session in sessions
                if session.agent_role is AgentRole.WATERBODY
            ]
            summarizers = [
                session
                for session in sessions
                if session.agent_role is AgentRole.RUN_SUMMARIZER
            ]
            active = {comid for path in paths for comid in path}

            assert snapshot.status is RunStatus.COMPLETED
            assert snapshot.analysis_phase.value == "terminal"
            assert snapshot.graph_outcome.coverage is coverage
            assert snapshot.result_ref is not None
            assert snapshot.summary.status.value == "completed"
            assert snapshot.summary.result_ref is not None
            assert len(waterbodies) == len(active)
            assert {session.target_comid for session in waterbodies} == active
            assert len(runtime.node_factory.requests) == len(active)
            assert len(runtime.node_factory.manifests) == len(active)
            assert len({item.node.comid for item in runtime.node_factory.requests}) == len(
                active
            )
            assert set(runtime.node_factory.model_calls) == model_calls
            assert len(summarizers) == 1
            assert summarizers[0].result_ref == snapshot.summary.result_ref
            assert runtime.summary_stage.waterbodies_terminal_before_create
            assert runtime.summary_stage.model_calls == 1
            assert runtime.summary_stage.tool_calls == 0
            assert runtime.model_router.models == [WATERBODY_MODEL]
            target_request = next(
                request
                for request in runtime.node_factory.requests
                if request.node.comid == TARGET
            )
            assert {
                dependency.status for dependency in target_request.dependency_outcomes
            } == direct_statuses
            target_manifest = next(
                manifest
                for manifest in runtime.node_factory.manifests
                if manifest.target_comid == TARGET
            )
            assert {
                dependency.dependency_status
                for dependency in target_manifest.dependencies
            } == direct_statuses

    asyncio.run(exercise())


def test_provider_cancelled_branch_fails_without_cancelling_target_workflow() -> None:
    async def exercise() -> None:
        async with complete_workflow_runtime(
            (("100", TARGET), ("200", TARGET)),
            {"200": DependencyStatus.USABLE, TARGET: DependencyStatus.INSUFFICIENT},
            node_factory_class=ProviderCancelledBranchFactory,
        ) as runtime:
            created, snapshot = await run_to_quiescence(runtime)
            sessions = await runtime.coordinator.list_sessions(created.run_id)
            branch = next(item for item in sessions if item.target_comid == "100")
            target = next(item for item in sessions if item.target_comid == TARGET)
            target_request = next(
                item for item in runtime.node_factory.requests
                if item.node.comid == TARGET
            )

            assert runtime.node_factory.provider_outcome.error.code == (
                "waterbody_model_cancelled"
            )
            assert branch.status is AgentSessionStatus.FAILED
            assert target.status is AgentSessionStatus.COMPLETED
            assert {
                item.status for item in target_request.dependency_outcomes
            } == {DependencyStatus.INVALID, DependencyStatus.USABLE}
            assert snapshot.status is RunStatus.COMPLETED
            assert snapshot.result_ref is not None

    asyncio.run(exercise())


def test_target_failure_skips_summary_while_summary_failure_preserves_target() -> None:
    async def target_failure() -> None:
        async with complete_workflow_runtime(
            (("100", TARGET),),
            {
                "100": DependencyStatus.USABLE,
                TARGET: DependencyStatus.INVALID,
            },
        ) as runtime:
            _, snapshot = await run_to_quiescence(runtime)
            assert snapshot.status is RunStatus.FAILED
            assert snapshot.result_ref is None
            assert runtime.summary_stage.calls == []

    async def summary_failure() -> None:
        async with complete_workflow_runtime(
            (("100", TARGET),),
            {
                "100": DependencyStatus.USABLE,
                TARGET: DependencyStatus.USABLE,
            },
            summary_failed=True,
        ) as runtime:
            _, snapshot = await run_to_quiescence(runtime)
            assert snapshot.status is RunStatus.COMPLETED
            assert snapshot.result_ref is not None
            assert snapshot.summary.status.value == "failed"
            assert snapshot.summary.error.code == "run_summary_failed"
            assert snapshot.summary.result_ref is None
            assert runtime.summary_stage.model_calls == 1

    asyncio.run(target_failure())
    asyncio.run(summary_failure())


def test_provider_cancelled_summary_fails_only_summary_when_run_is_not_cancelled() -> None:
    async def exercise() -> None:
        async with complete_workflow_runtime(
            (("100", TARGET),),
            {
                "100": DependencyStatus.USABLE,
                TARGET: DependencyStatus.USABLE,
            },
            summary_provider_cancelled=True,
        ) as runtime:
            created, snapshot = await run_to_quiescence(runtime)
            sessions = await runtime.coordinator.list_sessions(created.run_id)
            summarizers = [
                session
                for session in sessions
                if session.agent_role is AgentRole.RUN_SUMMARIZER
            ]

            assert snapshot.status is RunStatus.COMPLETED
            assert snapshot.result_ref is not None
            assert snapshot.summary.status.value == "failed"
            assert snapshot.summary.error.code == "run_summary_failed"
            assert snapshot.summary.error.retryable is True
            assert snapshot.summary.result_ref is None
            assert len(summarizers) == 1
            assert summarizers[0].status is AgentSessionStatus.FAILED
            assert summarizers[0].result_ref is None
            assert len(runtime.model_router.requests) == 1
            assert (
                '"input_tokens":0'
                in runtime.model_router.requests[0].messages[0].text
            )
            assert "[REDACTED]" not in runtime.model_router.requests[0].messages[0].text
            assert not runtime.coordinator._runs[created.run_id].stop_requested.is_set()

    asyncio.run(exercise())


class SummarySizeEstimator:
    def __init__(self) -> None:
        self.models = []

    def estimate(self, model, *, serialized_request, serialized_response_shape):
        self.models.append(model)
        assert serialized_request and serialized_response_shape
        return ModelSizeEstimate(
            input_tokens=20_000,
            output_tokens=1_000,
            estimated_output_bytes=8_000,
        )


def test_maximum_topology_is_exact_once_bounded_and_keeps_every_summary_card() -> None:
    async def exercise() -> None:
        graph = large_topology()
        release = asyncio.Event()
        probe = ConcurrencyProbe(4)
        runner = RecordingRunner(release=release, concurrency_probe=probe)
        scheduler = TopologyScheduler(
            run_id=RUN_ID,
            topology=graph,
            runner=runner,
            outcome_port=RecordingOutcomePort(),
            limiter=WaterbodyWorkflowLimiter(4),
        )
        scheduled_task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(probe.reached.wait(), timeout=2)
        assert probe.peak == 4
        release.set()
        scheduled = await asyncio.wait_for(scheduled_task, timeout=5)

        active = {node.comid for node in graph.nodes}
        assert len(active) == graph.limits.max_nodes == 128
        assert len(runner.requests) == 128
        assert {request.node.comid for request in runner.requests} == active
        assert len({outcome.session_id for outcome in scheduled.outcomes}) == 128

        reader = FakeResultReader()
        summary_outcomes = []
        for scheduled_outcome in scheduled.outcomes:
            result = inference_result(
                scheduled_outcome.target_comid,
                scheduled_outcome.session_id,
                statuses=(OutcomeStatus.INDETERMINATE,) * 2,
                result_id=f"result:{scheduled_outcome.target_comid}",
            )
            value = dependency_outcome(
                scheduled_outcome.target_comid,
                scheduled_outcome.session_id,
                status="insufficient",
                result=result,
                result_item_id=scheduled_outcome.result_ref.item_id,
            )
            assert value.target_comid == scheduled_outcome.target_comid
            assert value.session_id == scheduled_outcome.session_id
            assert value.result_ref == scheduled_outcome.result_ref
            reader.values[value.result_ref.item_id] = resolved_result(result)
            summary_outcomes.append(value)

        aggregation = TopologyResultAssembler(reader).aggregate(
            graph,
            tuple(summary_outcomes),
        )
        estimator = SummarySizeEstimator()
        summary_input = await RunSummaryInputBuilder(
            result_reader=reader,
            size_estimator=estimator,
        ).build(
            run_id=RUN_ID,
            session_id=uuid5(NAMESPACE_URL, "m7:max:summary"),
            query="Summarize the complete maximum topology.",
            topology=graph,
            topology_ref=TranscriptContentRef(
                run_id=RUN_ID,
                item_id=uuid5(NAMESPACE_URL, "m7:max:topology"),
            ),
            aggregation=aggregation,
            model=WATERBODY_MODEL,
            model_fit=ModelFitCapability(
                model=WATERBODY_MODEL,
                context_window_tokens=1_000_000,
                max_output_tokens=4_096,
            ),
        )
        cards = tuple(card.target_comid for card in summary_input.report_cards)
        assert len(cards) == 127
        assert len(set(cards)) == 127
        assert set(cards) == active - {graph.target_comid}
        assert summary_input.target_result_projection.target_comid == graph.target_comid
        assert estimator.models == [WATERBODY_MODEL]
        assert len(reader.calls) == 128
        assert "run_wide" not in AppSettings.model_fields

    asyncio.run(exercise())
