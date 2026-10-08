from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Callable
from uuid import UUID, uuid4

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    PublicError,
    SessionSummary,
)
from app.application.reasoning.batch_planner import (
    ModelFitCapability,
    ReasoningBatchPlanner,
)
from app.application.reasoning.draft_models import (
    overall_synthesis_response_schema,
    reasoning_batch_response_schema,
)
from app.application.reasoning.draft_validator import (
    DraftValidator,
    build_reasoning_evidence_catalog,
)
from app.application.reasoning.errors import (
    AllCellsBlockedError,
    DraftValidationError,
    InferenceAssemblyError,
    PlanningLoopError,
    ReasoningPlanTooLargeError,
    ValidationRepairFailedError,
)
from app.application.reasoning.planning_loop import (
    EvidencePlanningLoop,
    EvidencePlanningOutcome,
    EvidencePlanningStageCounts,
    PlanningConvergenceReason,
    build_reasoning_evidence_summaries,
    build_reasoning_working_context,
)
from app.application.evidence_tools import EvidenceAcquisitionHandoff
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.result_assembler import (
    assemble_inference_result,
    build_parameter_definitions,
)
from app.application.reasoning.result_models import build_result_skeleton
from app.application.reasoning.sufficiency import sealed_transcript_item_refs
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.application.reasoning.validation_repair import (
    ValidationExchange,
    ValidationExchangeAudit,
)
from app.domain import (
    EvidenceReference,
    InferenceResult,
    ModelExecutionSummary,
    ModelUsageSummary,
    ParameterCapabilityRegistry,
)
from app.ports.errors import WaterbodyModelPortError
from app.ports.waterbody_model import WaterbodyModelPort

if TYPE_CHECKING:
    from app.application.reasoning.transcript_store import ReasoningTranscriptStore


class WaterbodyAgentRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WaterbodyAgentRunOutcome:
    status: WaterbodyAgentRunStatus
    result: InferenceResult | None
    error: PublicError | None
    audits: tuple[ValidationExchangeAudit, ...]
    planning_counts: EvidencePlanningStageCounts | None

    def __post_init__(self) -> None:
        completed = self.status is WaterbodyAgentRunStatus.COMPLETED
        failed = self.status is WaterbodyAgentRunStatus.FAILED
        if completed != (self.result is not None):
            raise ValueError("completed runner outcome must contain exactly one result")
        if failed != (self.error is not None):
            raise ValueError("failed runner outcome must contain exactly one error")


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class RunSingleWaterbodyAgent:
    """Run exactly one validated waterbody task through the M5-M8 chain."""

    def __init__(
        self,
        *,
        planning_loop: EvidencePlanningLoop,
        capability_registry: ParameterCapabilityRegistry,
        batch_planner: ReasoningBatchPlanner,
        prompt_builder: PromptBuilder,
        model_port: WaterbodyModelPort,
        model_fit: ModelFitCapability,
        result_id_factory: Callable[[], str],
        error_id_factory: Callable[[], UUID] = uuid4,
        clock: object | None = None,
        transcript_store: "ReasoningTranscriptStore | None" = None,
    ) -> None:
        self._planning_loop = planning_loop
        self._capabilities = capability_registry
        self._batch_planner = batch_planner
        self._prompt_builder = prompt_builder
        self._model_port = model_port
        self._model_fit = model_fit
        self._result_id_factory = result_id_factory
        self._error_id_factory = error_id_factory
        self._clock = clock or _UtcClock()
        self._transcript_store = transcript_store

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline: datetime,
        initial_handoff: EvidenceAcquisitionHandoff | None = None,
    ) -> WaterbodyAgentRunOutcome:
        if self._transcript_store is not None:
            await self._transcript_store.write_agent_input(
                task,
                initial_evidence_bundle_ref=(
                    initial_handoff.bundle_ref
                    if initial_handoff is not None
                    else None
                ),
            )
        outcome = await self._run_reasoning(
            task,
            deadline=deadline,
            initial_handoff=initial_handoff,
        )
        if self._transcript_store is not None:
            if outcome.status is WaterbodyAgentRunStatus.COMPLETED:
                assert outcome.result is not None
                summary = await self._transcript_store.complete_with_result(
                    outcome.result
                )
            elif outcome.status is WaterbodyAgentRunStatus.FAILED:
                assert outcome.error is not None
                summary = await self._transcript_store.fail(outcome.error)
            else:
                summary = await self._transcript_store.cancel()
            return _committed_outcome(outcome, summary)
        return outcome

    async def _run_reasoning(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline: datetime,
        initial_handoff: EvidenceAcquisitionHandoff | None,
    ) -> WaterbodyAgentRunOutcome:
        audits: list[ValidationExchangeAudit] = []
        planning: EvidencePlanningOutcome | None = None
        stage = "evidence_planning"
        if task.task.waterbody_model != self._model_fit.model:
            raise ValueError("runner model-fit identity must match the task model")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("runner deadline must be timezone-aware")

        try:
            self._checkpoint(task, deadline, stage=stage)
            planning = (
                await self._planning_loop.run(task, deadline=deadline)
                if initial_handoff is None
                else await self._planning_loop.run(
                    task,
                    deadline=deadline,
                    initial_handoff=initial_handoff,
                )
            )
            if planning.counts.deadline_reached:
                return self._failed(
                    code="reasoning_deadline_exceeded",
                    message="The waterbody reasoning deadline was exceeded.",
                    stage=stage,
                    retryable=True,
                    audits=audits,
                    planning=planning,
                )
            self._checkpoint(task, deadline, stage="evidence_sealed")
            skeleton = build_result_skeleton(
                task,
                planning.sealed_input,
                self._capabilities,
                result_id=self._result_id_factory(),
                generated_at=self._clock.now(),
            )
            context = await build_reasoning_working_context(task, planning)
            evidence_summaries = build_reasoning_evidence_summaries(planning.handoff)
            stage = "batch_planning"
            self._checkpoint(task, deadline, stage=stage)
            plan = self._batch_planner.plan(
                skeleton,
                working_context=context,
                evidence_summaries=evidence_summaries,
                model_fit=self._model_fit,
                deadline=deadline,
                batch_response_schema=reasoning_batch_response_schema(),
                synthesis_response_schema=overall_synthesis_response_schema(),
            )

            reasoning = None
            synthesis = None
            if plan.batch_plan.batches:
                fragments = tuple(
                    [
                        await planning.handoff.read_port.read_fragment(
                            task.task.evidence_scope,
                            reference,
                        )
                        for reference in planning.handoff.fragment_allowlist
                    ]
                )
                catalog = build_reasoning_evidence_catalog(
                    planning.sealed_input,
                    fragments,
                )
                validator = DraftValidator(catalog)
                exchange = ValidationExchange(
                    model_port=self._model_port,
                    prompt_builder=self._prompt_builder,
                    validator=validator,
                    decision_store=self._transcript_store,
                )
                validated_batches = []
                stage = "reasoning_batch"
                for batch in plan.batch_plan.batches:
                    self._checkpoint(task, deadline, stage=stage)
                    planning = replace(
                        planning,
                        counts=replace(
                            planning.counts,
                            reasoning_batch_calls=(
                                planning.counts.reasoning_batch_calls + 1
                            ),
                        ),
                    )
                    try:
                        completed = await exchange.run_batch(
                            batch,
                            cancellation=task.task.cancellation_token,
                        )
                    except ValidationRepairFailedError as error:
                        audits.append(error.audit)
                        raise
                    audits.append(completed.audit)
                    validated_batches.append(completed.validated)
                    self._checkpoint(task, deadline, stage=stage)
                reasoning = validator.complete_batches(
                    plan.batch_plan,
                    tuple(validated_batches),
                )

                stage = "overall_synthesis"
                self._checkpoint(task, deadline, stage=stage)
                planning = replace(
                    planning,
                    counts=replace(
                        planning.counts,
                        synthesis_calls=planning.counts.synthesis_calls + 1,
                    ),
                )
                try:
                    completed_synthesis = await exchange.run_synthesis(
                        skeleton,
                        plan.batch_plan,
                        plan.synthesis_plan,
                        reasoning,
                        cancellation=task.task.cancellation_token,
                    )
                except ValidationRepairFailedError as error:
                    audits.append(error.audit)
                    raise
                audits.append(completed_synthesis.audit)
                synthesis = completed_synthesis.validated
                self._checkpoint(task, deadline, stage=stage)

            stage = "assembling_result"
            self._checkpoint(task, deadline, stage=stage)
            execution = _execution_summary(task, planning, audits, plan)
            result = assemble_inference_result(
                skeleton,
                reasoning=reasoning,
                synthesis=synthesis,
                parameter_definitions=build_parameter_definitions(
                    skeleton.requested_parameters,
                    self._capabilities,
                ),
                evidence_references=tuple(
                    EvidenceReference(
                        evidence_id=reference.evidence_id,
                        role="sealed_evidence",
                        summary=None,
                    )
                    for reference in planning.sealed_input.fragment_allowlist
                ),
                transcript_item_refs=sealed_transcript_item_refs(
                    planning.sealed_input
                ),
                model_execution_summary=execution,
            )
            self._checkpoint(task, deadline, stage=stage)
            return WaterbodyAgentRunOutcome(
                status=WaterbodyAgentRunStatus.COMPLETED,
                result=result,
                error=None,
                audits=tuple(audits),
                planning_counts=planning.counts,
            )
        except _RunCancelled:
            return WaterbodyAgentRunOutcome(
                status=WaterbodyAgentRunStatus.CANCELLED,
                result=None,
                error=None,
                audits=tuple(audits),
                planning_counts=planning.counts if planning is not None else None,
            )
        except _RunDeadline as error:
            return self._failed(
                code="reasoning_deadline_exceeded",
                message="The waterbody reasoning deadline was exceeded.",
                stage=error.stage,
                retryable=True,
                audits=audits,
                planning=planning,
            )
        except PlanningLoopError as error:
            if error.code == "planning_cancelled":
                return WaterbodyAgentRunOutcome(
                    status=WaterbodyAgentRunStatus.CANCELLED,
                    result=None,
                    error=None,
                    audits=tuple(audits),
                    planning_counts=None,
                )
            if error.code in {
                "evidence_prompt_projection_invalid",
                "evidence_prompt_projection_too_large",
            }:
                return self._failed(
                    code="reasoning_evidence_projection_failed",
                    message="The evidence could not be prepared for model reasoning.",
                    stage="evidence_planning",
                    retryable=False,
                    audits=audits,
                    planning=planning,
                )
            return self._failed(
                code="waterbody_model_response_invalid",
                message="The waterbody model returned an invalid response.",
                stage="evidence_planning",
                retryable=False,
                audits=audits,
                planning=planning,
            )
        except AllCellsBlockedError:
            budget_exhausted = (
                planning is not None
                and planning.counts.convergence_reason
                is PlanningConvergenceReason.TOOL_BUDGET_EXHAUSTED
            )
            return self._failed(
                code=(
                    "reasoning_budget_exceeded"
                    if budget_exhausted
                    else "reasoning_required_evidence_unavailable"
                ),
                message=(
                    "The evidence-planning budget was exhausted."
                    if budget_exhausted
                    else "Required evidence is unavailable for all requested cells."
                ),
                stage=("evidence_planning" if budget_exhausted else "evidence_sealed"),
                retryable=not budget_exhausted,
                audits=audits,
                planning=planning,
            )
        except ReasoningPlanTooLargeError:
            return self._failed(
                code="reasoning_plan_too_large",
                message="The complete reasoning plan exceeds the hard size limit.",
                stage="batch_planning",
                retryable=False,
                audits=audits,
                planning=planning,
            )
        except ValidationRepairFailedError:
            return self._failed(
                code="waterbody_model_repair_failed",
                message="The waterbody model repair response remained invalid.",
                stage="schema_repair",
                retryable=False,
                audits=audits,
                planning=planning,
            )
        except WaterbodyModelPortError as error:
            if (
                error.code == "waterbody_model_cancelled"
                and task.task.cancellation_token.is_requested()
            ):
                return WaterbodyAgentRunOutcome(
                    status=WaterbodyAgentRunStatus.CANCELLED,
                    result=None,
                    error=None,
                    audits=tuple(audits),
                    planning_counts=(
                        planning.counts if planning is not None else None
                    ),
                )
            code, message, retryable = _model_error(error)
            return self._failed(
                code=code,
                message=message,
                stage=stage,
                retryable=retryable,
                audits=audits,
                planning=planning,
            )
        except DraftValidationError:
            return self._failed(
                code="waterbody_model_response_invalid",
                message="The waterbody model returned an invalid response.",
                stage=stage,
                retryable=False,
                audits=audits,
                planning=planning,
            )
        except InferenceAssemblyError:
            return self._failed(
                code="inference_assembly_failed",
                message="The validated result could not be assembled.",
                stage="assembling_result",
                retryable=False,
                audits=audits,
                planning=planning,
            )

    def _checkpoint(
        self,
        task: ValidatedWaterbodyAgentTask,
        deadline: datetime,
        *,
        stage: str,
    ) -> None:
        if task.task.cancellation_token.is_requested():
            raise _RunCancelled()
        if self._clock.now() >= deadline:
            raise _RunDeadline(stage)

    def _failed(
        self,
        *,
        code: str,
        message: str,
        stage: str,
        retryable: bool,
        audits: list[ValidationExchangeAudit],
        planning: EvidencePlanningOutcome | None,
    ) -> WaterbodyAgentRunOutcome:
        return WaterbodyAgentRunOutcome(
            status=WaterbodyAgentRunStatus.FAILED,
            result=None,
            error=PublicError(
                error_id=self._error_id_factory(),
                code=code,
                message=message,
                failed_stage=stage,
                retryable=retryable,
            ),
            audits=tuple(audits),
            planning_counts=planning.counts if planning is not None else None,
        )


class _RunCancelled(Exception):
    pass


class _RunDeadline(Exception):
    def __init__(self, stage: str) -> None:
        self.stage = stage


def _committed_outcome(
    calculated: WaterbodyAgentRunOutcome,
    committed: SessionSummary,
) -> WaterbodyAgentRunOutcome:
    if committed.status is AgentSessionStatus.CANCELLED:
        return replace(
            calculated,
            status=WaterbodyAgentRunStatus.CANCELLED,
            result=None,
            error=None,
        )
    expected = {
        WaterbodyAgentRunStatus.COMPLETED: AgentSessionStatus.COMPLETED,
        WaterbodyAgentRunStatus.FAILED: AgentSessionStatus.FAILED,
        WaterbodyAgentRunStatus.CANCELLED: AgentSessionStatus.CANCELLED,
    }[calculated.status]
    if committed.status is not expected:
        raise RuntimeError("committed Agent session outcome does not match the runner")
    return calculated


def _execution_summary(task, planning, audits, plan) -> ModelExecutionSummary:
    usage = _aggregate_usage(planning.counts.usage, audits)
    return ModelExecutionSummary(
        model=task.task.waterbody_model,
        planning_calls=planning.counts.planning_model_calls,
        tool_calls=(
            planning.counts.preflight_tool_calls
            + planning.counts.agent_tool_calls
        ),
        reasoning_batches=plan.batch_plan.batch_count,
        repair_calls=sum(audit.repair_request is not None for audit in audits),
        synthesis_calls=int(plan.synthesis_plan.requires_model_call),
        skipped_cells=plan.synthesis_plan.coverage.fixed_cells,
        usage=usage,
    )


def _aggregate_usage(planning_usage, audits) -> ModelUsageSummary | None:
    input_tokens = 0
    output_tokens = 0
    reported = False
    if planning_usage is not None:
        reported = True
        input_tokens += planning_usage.input_tokens or 0
        output_tokens += planning_usage.output_tokens or 0
    for audit in audits:
        for response in (audit.initial_response, audit.repair_response):
            if response is not None and response.usage is not None:
                reported = True
                input_tokens += response.usage.input_tokens or 0
                output_tokens += response.usage.output_tokens or 0
    return (
        ModelUsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if reported
        else None
    )


def _model_error(error: WaterbodyModelPortError) -> tuple[str, str, bool]:
    if error.code == "waterbody_model_cancelled":
        return error.code, "The waterbody model request was cancelled by its provider.", True
    if error.code == "waterbody_model_unavailable":
        return error.code, "The waterbody model is unavailable.", True
    if error.code == "waterbody_model_timeout":
        return error.code, "The waterbody model request timed out.", True
    if error.code in {
        "waterbody_model_content_filtered",
        "waterbody_model_refused",
    }:
        return (
            "waterbody_model_content_filtered",
            "The waterbody model response was filtered.",
            False,
        )
    return (
        "waterbody_model_response_invalid",
        "The waterbody model returned an invalid response.",
        False,
    )
