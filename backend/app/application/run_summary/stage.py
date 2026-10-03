from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.application.analysis_runs.models import AgentRole, PublicError
from app.application.analysis_runs.topology_result_assembler import (
    WaterbodyOutcomeAggregation,
)
from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.reasoning.batch_planner import ModelFitCapability
from app.application.run_summary.errors import SummaryInputTooLargeError
from app.application.run_summary.input_builder import RunSummaryInputBuilder
from app.application.run_summary.runner import (
    CancellationProbe,
    RunSummaryRunOutcome,
    RunSummaryRunStatus,
    RunSummaryRunner,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain import HydrologyGraph, HydrologyParticipation, ModelSelection


logger = logging.getLogger("wateragents.run_summary")


class RunSummaryRunnerFactory(Protocol):
    def build(self, *, run_id: UUID, session_id: UUID) -> RunSummaryRunner: ...


class RunSummaryStage:
    """Create the one summarizer session only from a fully settled aggregation."""

    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        input_builder: RunSummaryInputBuilder,
        runner_factory: RunSummaryRunnerFactory,
    ) -> None:
        self._operations = operations
        self._input_builder = input_builder
        self._runner_factory = runner_factory

    async def run(
        self,
        *,
        run_id: UUID,
        query: str,
        topology: HydrologyGraph,
        topology_ref: TranscriptContentRef,
        aggregation: WaterbodyOutcomeAggregation,
        model: ModelSelection,
        model_fit: ModelFitCapability,
        deadline: datetime,
        cancellation: CancellationProbe,
    ) -> RunSummaryRunOutcome:
        active = {
            node.comid
            for node in topology.nodes
            if node.participation is HydrologyParticipation.ACTIVE
        }
        settled = {outcome.target_comid for outcome in aggregation.outcomes}
        if len(settled) != len(aggregation.outcomes) or settled != active:
            raise ValueError("run summarization requires all active outcomes")
        if cancellation.is_requested():
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.CANCELLED,
                result=None,
                error=None,
                model_calls=0,
                repaired=False,
            )

        session = await self._operations.prepare_agent_session(
            run_id,
            agent_role=AgentRole.RUN_SUMMARIZER,
            target_comid=None,
        )
        await self._operations.mark_agent_session_running(run_id, session.session_id)
        if cancellation.is_requested():
            await self._operations.cancel_agent_session(run_id, session.session_id)
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.CANCELLED,
                result=None,
                error=None,
                model_calls=0,
                repaired=False,
            )
        try:
            summary_input = await self._input_builder.build(
                run_id=run_id,
                session_id=session.session_id,
                query=query,
                topology=topology,
                topology_ref=topology_ref,
                aggregation=aggregation,
                model=model,
                model_fit=model_fit,
            )
        except SummaryInputTooLargeError:
            error = PublicError(
                error_id=uuid4(),
                code="summary_input_too_large",
                message="The complete run summary input is too large for the selected model.",
                failed_stage="run_summarization",
                retryable=False,
            )
            await self._operations.fail_agent_session(run_id, session.session_id, error)
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.FAILED,
                result=None,
                error=error,
                model_calls=0,
                repaired=False,
            )
        except Exception:
            logger.exception("run_summary_input_failed", extra={"run_id": str(run_id)})
            error = PublicError(
                error_id=uuid4(),
                code="run_summary_failed",
                message="The run summary input could not be prepared.",
                failed_stage="run_summarization",
                retryable=False,
            )
            await self._operations.fail_agent_session(run_id, session.session_id, error)
            return RunSummaryRunOutcome(
                status=RunSummaryRunStatus.FAILED,
                result=None,
                error=error,
                model_calls=0,
                repaired=False,
            )
        return await self._runner_factory.build(
            run_id=run_id,
            session_id=session.session_id,
        ).run(
            summary_input,
            model=model,
            deadline=deadline,
            cancellation=cancellation,
        )
