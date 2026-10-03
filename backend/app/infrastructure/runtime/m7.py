from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    RunStatus,
    RunSummaryStatus,
)
from app.application.analysis_runs.topology_driven_workflow import (
    ProductionTopologyNodeRunnerFactory,
    TopologyWaterbodyExecutionStage,
)
from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
    WaterbodyOutcomeAggregation,
)
from app.application.analysis_runs.topology_scheduler import (
    WaterbodyWorkflowLimiter,
)
from app.application.analysis_runs.topology_result_models import TopologySummaryResult
from app.application.analysis_runs.workflow import (
    WorkflowContext,
    WorkflowOperations,
    WorkflowStopped,
)
from app.application.evidence_tools import (
    RunLocalUpstreamResultRegistry,
    UpstreamInferenceResultReader,
)
from app.application.query_understanding import AnalysisContext
from app.application.run_summary.input_builder import RunSummaryInputBuilder
from app.application.run_summary.runner import RunSummaryRunStatus, RunSummaryRunner
from app.application.run_summary.stage import RunSummaryStage
from app.application.run_summary.transcript_store import RunSummaryTranscriptStore
from app.application.transcripts.models import TranscriptContentRef
from app.domain import HydrologyGraph, ModelCatalogSnapshot, ParameterCapabilityRegistry
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.runtime.m5 import M5Runtime
from app.infrastructure.runtime.m6 import M6Runtime
from app.ports.clock import ClockPort


class ProductionRunSummaryRunnerFactory:
    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        m6_runtime: M6Runtime,
        clock: ClockPort,
        operation_id_factory: Callable[[], UUID],
        error_id_factory: Callable[[], UUID],
    ) -> None:
        self._operations = operations
        self._m6_runtime = m6_runtime
        self._clock = clock
        self._operation_id_factory = operation_id_factory
        self._error_id_factory = error_id_factory

    def build(self, *, run_id: UUID, session_id: UUID) -> RunSummaryRunner:
        return RunSummaryRunner(
            model_port=self._m6_runtime.model_port_router,
            transcript_store=RunSummaryTranscriptStore(
                operations=self._operations,
                run_id=run_id,
                session_id=session_id,
                private_values=self._m6_runtime.private_values,
            ),
            operation_id_factory=self._operation_id_factory,
            error_id_factory=self._error_id_factory,
            clock=self._clock,
        )


class CompleteRunSummaryBoundary:
    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        summary_stage: RunSummaryStage,
        result_assembler: TopologyResultAssembler,
        m6_runtime: M6Runtime,
        clock: ClockPort,
        timeout_seconds: float,
    ) -> None:
        self._operations = operations
        self._summary_stage = summary_stage
        self._result_assembler = result_assembler
        self._m6_runtime = m6_runtime
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    async def wait(
        self,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        topology: HydrologyGraph,
        analysis_context_ref: TranscriptContentRef,
        topology_ref: TranscriptContentRef,
        aggregation: WaterbodyOutcomeAggregation,
    ) -> None:
        context.cancellation.checkpoint()
        model = analysis_context.waterbody_model
        outcome = await self._summary_stage.run(
            run_id=context.run.run_id,
            query=analysis_context.query,
            topology=topology,
            topology_ref=topology_ref,
            aggregation=aggregation,
            model=model,
            model_fit=self._m6_runtime.model_port_router.model_fit(model),
            deadline=self._clock.now() + timedelta(seconds=self._timeout_seconds),
            cancellation=context.cancellation,
        )
        if outcome.status is RunSummaryRunStatus.CANCELLED:
            raise WorkflowStopped()
        if outcome.session_id is None:
            raise RuntimeError("terminal run summary has no session identity")
        session = await self._operations.get_session(
            context.run.run_id,
            outcome.session_id,
        )
        if outcome.status is RunSummaryRunStatus.COMPLETED:
            if (
                session.status is not AgentSessionStatus.COMPLETED
                or session.result_ref is None
            ):
                raise RuntimeError("completed run summary is not committed")
            summary = TopologySummaryResult(
                status=RunSummaryStatus.COMPLETED,
                session_id=session.session_id,
                result_ref=session.result_ref,
            )
        else:
            if (
                session.status is not AgentSessionStatus.FAILED
                or outcome.error is None
            ):
                raise RuntimeError("failed run summary is not committed")
            summary = TopologySummaryResult(
                status=RunSummaryStatus.FAILED,
                session_id=session.session_id,
                public_error=outcome.error,
            )
        context.cancellation.checkpoint()
        result = self._result_assembler.build_run_result(
            run_id=context.run.run_id,
            analysis_context_ref=analysis_context_ref,
            topology_ref=topology_ref,
            aggregation=aggregation,
            summary=summary,
            generated_at=self._clock.now(),
        )
        context.cancellation.checkpoint()
        snapshot = await self._operations.complete_run(context.run.run_id, result)
        if snapshot.status is RunStatus.CANCELLED:
            raise WorkflowStopped()


def compose_m7_execution_stage(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    m5_runtime: M5Runtime,
    m6_runtime: M6Runtime,
    upstream_results: RunLocalUpstreamResultRegistry,
    result_reader: UpstreamInferenceResultReader,
    capability_registry: ParameterCapabilityRegistry,
    model_catalog: ModelCatalogSnapshot,
    clock: ClockPort,
    result_id_factory: Callable[[], str],
    error_id_factory: Callable[[], UUID],
    operation_id_factory: Callable[[], UUID],
) -> TopologyWaterbodyExecutionStage:
    if not m6_runtime.model_port_router.providers:
        raise SettingsConfigurationError(
            "M7 runtime requires a configured waterbody model provider"
        )
    assembler = TopologyResultAssembler(result_reader)
    node_runner_factory = ProductionTopologyNodeRunnerFactory(
        operations=operations,
        acquisition=m5_runtime.acquisition,
        session_reader=m5_runtime.transcript,
        waterbody_runner=m6_runtime.runner,
        result_assembler=assembler,
        capability_registry=capability_registry,
        model_catalog=model_catalog,
        result_id_factory=result_id_factory,
        error_id_factory=error_id_factory,
        clock=clock,
    )
    runner_factory = ProductionRunSummaryRunnerFactory(
        operations=operations,
        m6_runtime=m6_runtime,
        clock=clock,
        operation_id_factory=operation_id_factory,
        error_id_factory=error_id_factory,
    )
    summary_stage = RunSummaryStage(
        operations=operations,
        input_builder=RunSummaryInputBuilder(
            result_reader=result_reader,
            size_estimator=m6_runtime.model_port_router,
        ),
        runner_factory=runner_factory,
        error_id_factory=error_id_factory,
    )
    return TopologyWaterbodyExecutionStage(
        operations=operations,
        outcome_stores=upstream_results,
        node_runner_factory=node_runner_factory,
        result_assembler=assembler,
        limiter=WaterbodyWorkflowLimiter(
            settings.waterbody_workflow_max_concurrency
        ),
        summary_boundary=CompleteRunSummaryBoundary(
            operations=operations,
            summary_stage=summary_stage,
            result_assembler=assembler,
            m6_runtime=m6_runtime,
            clock=clock,
            timeout_seconds=settings.run_summary_timeout_seconds,
        ),
    )
