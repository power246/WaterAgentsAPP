import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    PublicError,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    GraphCoverage,
    OutcomeCounts,
    TopologyRunResult,
    TopologySummaryResult,
    WaterbodyRunEntry,
)
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.run_summary.models import RunSummaryAgentMessage, RunSummaryResult
from app.application.transcripts.models import GenerationStatus, MessageEndReason
from app.domain import InferenceResult
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.analysis_runs._topology_scheduler_support import topology
from tests.domain.test_results import make_result
from tests.m4_support import MODEL_CATALOG, WATERBODY_MODEL, analysis_context, create_request_bytes


class StepClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 3, 8, tzinfo=UTC)

    def now(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


@dataclass(frozen=True, slots=True)
class CompletedM7Run:
    run_id: UUID
    context_item_id: UUID
    topology_item_id: UUID
    summary_session_id: UUID
    topology_result: TopologyRunResult
    ready_statuses: tuple[str, ...]


def coordinator_for(store, workflow: HoldingWorkflow, **options) -> RunCoordinator:
    return RunCoordinator(
        StepClock(),
        model_catalog=MODEL_CATALOG,
        transcript_store=store,
        workflow=workflow,
        shutdown_grace_period=0,
        shutdown_save_grace_period=options.pop("shutdown_save_grace_period", 1),
        persistence_retry_delays=options.pop(
            "persistence_retry_delays",
            (0, 0, 0, 0, 0),
        ),
        live_state_grace_period=options.pop("live_state_grace_period", 60),
        **options,
    )


def summary_message_for(result: RunSummaryResult) -> RunSummaryAgentMessage:
    return RunSummaryAgentMessage(
        run_id=result.run_id,
        session_id=result.session_id,
        message_id="validated-summary",
        operation_id=UUID("d5a64a52-c1e2-4725-88ee-6c83f105e636"),
        text=result.plain_language_summary,
        generation_status=GenerationStatus.COMPLETED,
        end_reason=MessageEndReason.COMPLETED,
    )


async def prepare_m7_run(
    coordinator: RunCoordinator,
    workflow: HoldingWorkflow,
    *,
    partial: bool = False,
    summary_failed: bool = False,
) -> CompletedM7Run:
    created = await coordinator.create_run(create_request_bytes("M7 projection"))
    await workflow.calls.get()

    parser = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.QUERY_PARSER,
    )
    await coordinator.mark_agent_session_running(created.run_id, parser.session_id)
    context_identity = await coordinator.reserve_transcript_item(
        created.run_id,
        session_id=parser.session_id,
    )
    await coordinator.finalize_transcript_item(
        created.run_id,
        context_identity.item_id,
        "query_understood",
        1,
        analysis_context("M7 projection"),
    )
    await coordinator.complete_agent_session(created.run_id, parser.session_id)

    graph = topology(("100", "300"))
    topology_identity = await coordinator.reserve_transcript_item(created.run_id)
    await coordinator.finalize_transcript_item(
        created.run_id,
        topology_identity.item_id,
        "topology_ready",
        1,
        graph,
    )
    context_ref, topology_ref = await coordinator.begin_waterbody_execution(
        created.run_id
    )

    entries: list[WaterbodyRunEntry] = []
    ready_statuses: list[str] = []
    for position, comid in enumerate(("100", "300")):
        await coordinator.mark_waterbody_ready(created.run_id, comid)
        stream = await coordinator.subscribe_run(created.run_id)
        ready_snapshot = await anext(stream)
        await stream.aclose()
        ready_statuses.append(next(
            item.scheduling_status.value
            for item in ready_snapshot.waterbodies
            if item.target_comid == comid
        ))
        session = await coordinator.prepare_agent_session(
            created.run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid=comid,
        )
        await coordinator.mark_agent_session_running(created.run_id, session.session_id)
        if partial and comid == "100":
            await coordinator.fail_agent_session(
                created.run_id,
                session.session_id,
                PublicError(
                    error_id=uuid4(),
                    code="waterbody_execution_failed",
                    message="The upstream waterbody analysis failed.",
                    failed_stage="waterbody_execution",
                    retryable=False,
                ),
            )
            entries.append(WaterbodyRunEntry(
                graph_position=position,
                target_comid=comid,
                session_id=session.session_id,
                dependency_status=DependencyStatus.INVALID,
                session_status=AgentSessionStatus.FAILED,
            ))
            continue
        result = _inference_result(created.run_id, session.session_id, comid)
        completed = await coordinator.complete_waterbody_agent_session(
            created.run_id,
            session.session_id,
            result,
        )
        entries.append(WaterbodyRunEntry(
            graph_position=position,
            target_comid=comid,
            session_id=session.session_id,
            dependency_status=DependencyStatus.USABLE,
            session_status=AgentSessionStatus.COMPLETED,
            result_ref=completed.result_ref,
            result_summary=completed.result_summary,
        ))

    await coordinator.begin_run_summarization(created.run_id)
    summary_session = await coordinator.prepare_agent_session(
        created.run_id,
        agent_role=AgentRole.RUN_SUMMARIZER,
    )
    await coordinator.mark_agent_session_running(
        created.run_id,
        summary_session.session_id,
    )
    if summary_failed:
        summary_error = PublicError(
            error_id=uuid4(),
            code="run_summary_failed",
            message="The plain-language summary could not be generated.",
            failed_stage="run_summarization",
            retryable=True,
        )
        await coordinator.fail_agent_session(
            created.run_id,
            summary_session.session_id,
            summary_error,
        )
        summary = TopologySummaryResult(
            status="failed",
            session_id=summary_session.session_id,
            public_error=summary_error,
        )
    else:
        summary_result = RunSummaryResult(
            run_id=created.run_id,
            session_id=summary_session.session_id,
            input_digest="sha256:m7-projection",
            model=WATERBODY_MODEL,
            language="en",
            plain_language_summary="The target result is available.",
            generated_at=datetime(2026, 10, 3, 8, 30, tzinfo=UTC),
        )
        completed_summary = await coordinator.complete_run_summary_agent_session(
            created.run_id,
            summary_session.session_id,
            summary_result,
            message=summary_message_for(summary_result),
        )
        summary = TopologySummaryResult(
            status="completed",
            session_id=summary_session.session_id,
            result_ref=completed_summary.result_ref,
        )

    target = entries[-1]
    counts = OutcomeCounts(
        usable=sum(item.dependency_status is DependencyStatus.USABLE for item in entries),
        invalid=sum(item.dependency_status is DependencyStatus.INVALID for item in entries),
    )
    topology_result = TopologyRunResult(
        run_id=created.run_id,
        analysis_context_ref=context_ref,
        topology_ref=topology_ref,
        target_comid="300",
        target_result_ref=target.result_ref,
        target_result_summary=target.result_summary,
        graph_coverage=GraphCoverage.PARTIAL if partial else GraphCoverage.COMPLETE,
        node_outcomes=tuple(entries),
        outcome_counts=counts,
        summary=summary,
        generated_at=datetime(2026, 10, 3, 8, 31, tzinfo=UTC),
    )
    return CompletedM7Run(
        run_id=created.run_id,
        context_item_id=context_identity.item_id,
        topology_item_id=topology_identity.item_id,
        summary_session_id=summary_session.session_id,
        topology_result=topology_result,
        ready_statuses=tuple(ready_statuses),
    )


async def complete_m7_run(
    coordinator: RunCoordinator,
    workflow: HoldingWorkflow,
    *,
    partial: bool = False,
    summary_failed: bool = False,
) -> CompletedM7Run:
    prepared = await prepare_m7_run(
        coordinator,
        workflow,
        partial=partial,
        summary_failed=summary_failed,
    )
    await coordinator.complete_run(prepared.run_id, prepared.topology_result)
    workflow.release.set()
    async with asyncio.timeout(2):
        await coordinator._runs[prepared.run_id].workflow_quiesced.wait()
        await coordinator._transcript_writer.drain(coordinator._runs[prepared.run_id])
    return prepared


def _inference_result(run_id: UUID, session_id: UUID, comid: str) -> InferenceResult:
    values = make_result().model_dump(mode="python")
    values.update(
        run_id=run_id,
        session_id=session_id,
        target_comid=comid,
        result_id=f"result-{comid}",
    )
    return InferenceResult.model_validate(values)
