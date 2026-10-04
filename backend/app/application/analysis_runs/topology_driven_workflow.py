from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    PublicError,
)
from app.application.analysis_runs.node_launch import (
    NodeLaunchAction,
    NodeLaunchPolicy,
    build_node_launch_facts,
)
from app.application.analysis_runs.topology_result_assembler import (
    TargetWaterbodyCancelledError,
    TargetWaterbodyFailedError,
    TopologyResultAssembler,
    WaterbodyOutcomeAggregation,
)
from app.application.analysis_runs.topology_scheduler import (
    NodeExecutionRequest,
    NodeRunner,
    TopologyExecutionInvalidError,
    TopologyScheduler,
    WaterbodyWorkflowLimiter,
)
from app.application.analysis_runs.workflow import (
    WorkflowContext,
    WorkflowFailed,
    WorkflowOperations,
    WorkflowStopped,
)
from app.application.analysis_runs.zero_model_result import (
    DeterministicInsufficientResultAssembler,
)
from app.application.evidence_tools import (
    EvidenceAcquisitionHandoff,
    EvidenceSessionScope,
    EvidenceToolReader,
    RunLocalUpstreamResultRegistry,
    RunLocalUpstreamResultStore,
    assemble_evidence_session_scope,
)
from app.application.query_understanding import AnalysisContext
from app.application.reasoning import (
    ValidatedWaterbodyAgentTask,
    WaterbodyAgentRunOutcome,
    WaterbodyAgentTask,
    validate_waterbody_agent_task,
)
from app.application.reasoning.transcript_store import ReasoningTranscriptStore
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    HydrologyGraph,
    ItemReference,
    ModelCatalogSnapshot,
    ParameterCapabilityRegistry,
    ReferenceScope,
)


class EvidenceAcquisitionPort(Protocol):
    async def start(
        self,
        scope: EvidenceSessionScope,
    ) -> EvidenceAcquisitionHandoff: ...


class WaterbodyRunnerPort(Protocol):
    def session_deadline(self): ...

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline,
        initial_handoff: EvidenceAcquisitionHandoff | None = None,
    ) -> WaterbodyAgentRunOutcome: ...


class TopologyNodeRunnerFactory(Protocol):
    def build(
        self,
        *,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        topology: HydrologyGraph,
        analysis_context_ref: TranscriptContentRef,
        topology_ref: TranscriptContentRef,
        outcome_store: RunLocalUpstreamResultStore,
    ) -> NodeRunner: ...


class RunSummaryBoundary(Protocol):
    async def wait(
        self,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        topology: HydrologyGraph,
        analysis_context_ref: TranscriptContentRef,
        topology_ref: TranscriptContentRef,
        aggregation: WaterbodyOutcomeAggregation,
    ) -> None: ...


class TopologyWaterbodyExecutionStage:
    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        outcome_stores: RunLocalUpstreamResultRegistry,
        node_runner_factory: TopologyNodeRunnerFactory,
        result_assembler: TopologyResultAssembler,
        limiter: WaterbodyWorkflowLimiter,
        summary_boundary: RunSummaryBoundary,
    ) -> None:
        self._operations = operations
        self._outcome_stores = outcome_stores
        self._node_runner_factory = node_runner_factory
        self._result_assembler = result_assembler
        self._limiter = limiter
        self._summary_boundary = summary_boundary

    async def execute(
        self,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        graph: HydrologyGraph,
        *,
        topology_item_id: UUID,
    ) -> None:
        context.cancellation.checkpoint()
        analysis_context_ref, topology_ref = (
            await self._operations.begin_waterbody_execution(context.run.run_id)
        )
        if topology_ref.item_id != topology_item_id:
            raise WorkflowFailed(
                "The saved topology identity changed before execution.",
                code="topology_execution_invalid",
                public_message="The saved topology could not be executed.",
                failed_stage="waterbody_execution",
                retryable=False,
            )
        outcome_store = await self._outcome_stores.open_run(
            run_id=context.run.run_id,
            topology=graph,
            topology_ref=topology_ref,
        )
        node_runner = self._node_runner_factory.build(
            context=context,
            analysis_context=analysis_context,
            topology=graph,
            analysis_context_ref=analysis_context_ref,
            topology_ref=topology_ref,
            outcome_store=outcome_store,
        )
        try:
            scheduled = await TopologyScheduler(
                run_id=context.run.run_id,
                topology=graph,
                runner=node_runner,
                outcome_port=outcome_store,
                limiter=self._limiter,
                cancellation=context.cancellation,
                readiness_port=self._operations,
                outcome_observer=self._operations,
            ).run()
            context.cancellation.checkpoint()
            aggregation = self._result_assembler.aggregate(
                graph,
                scheduled.outcomes,
            )
        except TargetWaterbodyFailedError as error:
            raise WorkflowFailed(
                "The target waterbody did not produce a committed result.",
                code="target_waterbody_failed",
                public_message="The target waterbody analysis failed.",
                failed_stage="waterbody_execution",
                retryable=False,
            ) from error
        except TargetWaterbodyCancelledError as error:
            if context.cancellation.is_requested():
                raise WorkflowStopped() from error
            raise WorkflowFailed(
                "The target waterbody session was cancelled.",
                code="target_waterbody_cancelled",
                public_message="The target waterbody analysis was cancelled.",
                failed_stage="waterbody_execution",
                retryable=False,
            ) from error
        except TopologyExecutionInvalidError as error:
            raise WorkflowFailed(
                "The saved topology could not be executed.",
                code="topology_execution_invalid",
                public_message="The saved topology could not be executed.",
                failed_stage="waterbody_execution",
                retryable=False,
            ) from error

        context.cancellation.checkpoint()
        await self._operations.begin_run_summarization(context.run.run_id)
        context.cancellation.checkpoint()
        await self._summary_boundary.wait(
            context,
            analysis_context,
            graph,
            analysis_context_ref,
            topology_ref,
            aggregation,
        )


class ProductionTopologyNodeRunnerFactory:
    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        acquisition: EvidenceAcquisitionPort,
        session_reader: EvidenceToolReader,
        waterbody_runner: WaterbodyRunnerPort,
        result_assembler: TopologyResultAssembler,
        capability_registry: ParameterCapabilityRegistry,
        model_catalog: ModelCatalogSnapshot,
        result_id_factory: Callable[[], str],
        error_id_factory: Callable[[], UUID],
        clock: object,
    ) -> None:
        self._operations = operations
        self._acquisition = acquisition
        self._session_reader = session_reader
        self._waterbody_runner = waterbody_runner
        self._result_assembler = result_assembler
        self._capability_registry = capability_registry
        self._model_catalog = model_catalog
        self._result_id_factory = result_id_factory
        self._error_id_factory = error_id_factory
        self._clock = clock

    def build(
        self,
        *,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        topology: HydrologyGraph,
        analysis_context_ref: TranscriptContentRef,
        topology_ref: TranscriptContentRef,
        outcome_store: RunLocalUpstreamResultStore,
    ) -> NodeRunner:
        return ProductionTopologyNodeRunner(
            context=context,
            analysis_context=analysis_context,
            topology=topology,
            analysis_context_ref=analysis_context_ref,
            topology_ref=topology_ref,
            outcome_store=outcome_store,
            operations=self._operations,
            acquisition=self._acquisition,
            session_reader=self._session_reader,
            waterbody_runner=self._waterbody_runner,
            result_assembler=self._result_assembler,
            capability_registry=self._capability_registry,
            model_catalog=self._model_catalog,
            zero_model=DeterministicInsufficientResultAssembler(
                capability_registry=self._capability_registry,
                result_id_factory=self._result_id_factory,
                clock=self._clock,
            ),
            error_id_factory=self._error_id_factory,
        )


class ProductionTopologyNodeRunner:
    def __init__(
        self,
        *,
        context: WorkflowContext,
        analysis_context: AnalysisContext,
        topology: HydrologyGraph,
        analysis_context_ref: TranscriptContentRef,
        topology_ref: TranscriptContentRef,
        outcome_store: RunLocalUpstreamResultStore,
        operations: WorkflowOperations,
        acquisition: EvidenceAcquisitionPort,
        session_reader: EvidenceToolReader,
        waterbody_runner: WaterbodyRunnerPort,
        result_assembler: TopologyResultAssembler,
        capability_registry: ParameterCapabilityRegistry,
        model_catalog: ModelCatalogSnapshot,
        zero_model: DeterministicInsufficientResultAssembler,
        error_id_factory: Callable[[], UUID],
    ) -> None:
        self._context = context
        self._analysis_context = analysis_context
        self._topology = topology
        self._analysis_context_ref = analysis_context_ref
        self._topology_ref = topology_ref
        self._outcome_store = outcome_store
        self._operations = operations
        self._acquisition = acquisition
        self._session_reader = session_reader
        self._waterbody_runner = waterbody_runner
        self._result_assembler = result_assembler
        self._capability_registry = capability_registry
        self._model_catalog = model_catalog
        self._zero_model = zero_model
        self._error_id_factory = error_id_factory
        self._session_ids: dict[str, UUID] = {}

    async def run(self, request: NodeExecutionRequest):
        self._context.cancellation.checkpoint()
        session = await self._operations.prepare_agent_session(
            request.run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid=request.node.comid,
        )
        self._session_ids[request.node.comid] = session.session_id
        self._context.cancellation.checkpoint()
        manifest = await self._outcome_store.build_manifest(
            session_id=session.session_id,
            target_comid=request.node.comid,
        )
        self._context.cancellation.checkpoint()
        manifest_item = await self._operations.reserve_transcript_item(
            request.run_id,
            session_id=session.session_id,
        )
        self._context.cancellation.checkpoint()
        await self._operations.finalize_transcript_item(
            request.run_id,
            manifest_item.item_id,
            "waterbody_dependency_manifest",
            1,
            manifest,
        )
        self._context.cancellation.checkpoint()
        scope = assemble_evidence_session_scope(
            run_id=str(request.run_id),
            session_id=str(session.session_id),
            session_fact=ReferenceScope(
                item_id=str(session.session_id),
                run_id=str(request.run_id),
                session_id=str(session.session_id),
            ),
            analysis_context_ref=ReferenceScope(
                item_id=str(self._analysis_context_ref.item_id),
                run_id=str(request.run_id),
            ),
            topology_ref=ReferenceScope(
                item_id=str(self._topology_ref.item_id),
                run_id=str(request.run_id),
            ),
            context=self._analysis_context,
            topology=self._topology,
            agent_comid=request.node.comid,
            registry=self._capability_registry,
            cancel_token=self._context.cancellation,
        )
        task = await validate_waterbody_agent_task(
            WaterbodyAgentTask(
                run_id=request.run_id,
                session_id=session.session_id,
                target_comid=request.node.comid,
                analysis_context_ref=ItemReference(
                    item_id=str(self._analysis_context_ref.item_id)
                ),
                topology_ref=ItemReference(item_id=str(self._topology_ref.item_id)),
                evidence_scope=scope,
                waterbody_model=self._analysis_context.waterbody_model,
                memory_mode=self._analysis_context.memory_mode,
                cancellation_token=self._context.cancellation,
                dependency_manifest=manifest,
            ),
            run=self._context.run,
            context=self._analysis_context,
            topology=self._topology,
            model_catalog=self._model_catalog,
            session_reader=self._session_reader,
        )
        self._context.cancellation.checkpoint()
        await self._operations.mark_agent_session_running(
            request.run_id,
            session.session_id,
        )
        self._context.cancellation.checkpoint()
        handoff = await self._acquisition.start(scope)
        self._context.cancellation.checkpoint()
        decision = NodeLaunchPolicy().decide(
            build_node_launch_facts(task, handoff),
            manifest,
        )
        if decision.action is NodeLaunchAction.RUN_AGENT:
            self._context.cancellation.checkpoint()
            await self._waterbody_runner.run(
                task,
                deadline=self._waterbody_runner.session_deadline(),
                initial_handoff=handoff,
            )
        elif decision.action is NodeLaunchAction.DETERMINISTIC_INSUFFICIENT:
            self._context.cancellation.checkpoint()
            await self._zero_model.run(
                task,
                handoff,
                transcript_store=ReasoningTranscriptStore(
                    operations=self._operations,
                    run_id=request.run_id,
                    session_id=session.session_id,
                ),
            )
        elif decision.action is NodeLaunchAction.CANCELLED:
            await self._operations.cancel_agent_session(
                request.run_id,
                session.session_id,
            )
        else:
            await self._operations.fail_agent_session(
                request.run_id,
                session.session_id,
                self._public_error(
                    "waterbody_launch_invalid",
                    "The waterbody evidence boundary was invalid.",
                ),
            )
        if self._context.cancellation.is_requested():
            raise asyncio.CancelledError()
        return await self._authoritative_outcome(request.node.comid)

    async def settle_failure(
        self,
        request: NodeExecutionRequest,
        error: Exception,
    ):
        del error
        session_id = self._session_ids.get(request.node.comid)
        if session_id is None:
            if self._context.cancellation.is_requested():
                raise asyncio.CancelledError()
            raise RuntimeError("node failed before its session was created")
        session = await self._operations.get_session(request.run_id, session_id)
        if self._context.cancellation.is_requested():
            if session.status in {
                AgentSessionStatus.PENDING,
                AgentSessionStatus.RUNNING,
            }:
                await self._operations.cancel_agent_session(
                    request.run_id,
                    session_id,
                )
            raise asyncio.CancelledError()
        if session.status in {
            AgentSessionStatus.PENDING,
            AgentSessionStatus.RUNNING,
        }:
            await self._operations.fail_agent_session(
                request.run_id,
                session_id,
                self._public_error(
                    "waterbody_execution_failed",
                    "The waterbody analysis failed.",
                ),
            )
        return await self._authoritative_outcome(request.node.comid)

    async def _authoritative_outcome(self, target_comid: str):
        session_id = self._session_ids[target_comid]
        session = await self._operations.get_session(
            self._context.run.run_id,
            session_id,
        )
        if self._context.cancellation.is_requested():
            raise asyncio.CancelledError()
        outcome = await self._result_assembler.dependency_outcome(
            self._context.run.run_id,
            session,
        )
        if self._context.cancellation.is_requested():
            raise asyncio.CancelledError()
        return outcome

    def _public_error(self, code: str, message: str) -> PublicError:
        return PublicError(
            error_id=self._error_id_factory(),
            code=code,
            message=message,
            failed_stage="waterbody_execution",
            retryable=False,
        )
