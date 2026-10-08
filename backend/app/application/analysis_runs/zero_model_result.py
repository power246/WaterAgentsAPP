from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
)
from app.application.evidence_tools import EvidenceAcquisitionHandoff
from app.application.reasoning.result_assembler import (
    assemble_inference_result,
    build_parameter_definitions,
)
from app.application.reasoning.result_models import build_result_skeleton
from app.application.reasoning.sufficiency import (
    ReasoningGateDecision,
    seal_evidence_input,
    sealed_transcript_item_refs,
)
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.application.reasoning.transcript_store import ReasoningTranscriptStore
from app.domain import (
    EvidenceReference,
    InferenceResult,
    ModelExecutionSummary,
    ParameterCapabilityRegistry,
)


@dataclass(frozen=True, slots=True)
class ZeroModelResultOutcome:
    result: InferenceResult | None
    session: SessionSummary
    dependency_outcome: DependencyOutcome
    planning_calls: int = 0
    final_calls: int = 0
    optional_tool_calls: int = 0


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DeterministicInsufficientResultAssembler:
    """Commit a complete insufficient result without calling a model."""

    def __init__(
        self,
        *,
        capability_registry: ParameterCapabilityRegistry,
        result_id_factory: Callable[[], str],
        clock: object | None = None,
    ) -> None:
        self._capabilities = capability_registry
        self._result_id_factory = result_id_factory
        self._clock = clock or _UtcClock()

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        handoff: EvidenceAcquisitionHandoff,
        *,
        transcript_store: ReasoningTranscriptStore,
    ) -> ZeroModelResultOutcome:
        if not handoff.stop_before_model or handoff.block_facts:
            raise ValueError("zero-model result requires a natural insufficient gate")
        sealed = seal_evidence_input(task, handoff, self._capabilities)
        if not sealed.gates or any(
            gate.decision is not ReasoningGateDecision.UNSUPPORTED
            for gate in sealed.gates
        ):
            raise ValueError("zero-model result requires only unsupported cells")
        skeleton = build_result_skeleton(
            task,
            sealed,
            self._capabilities,
            result_id=self._result_id_factory(),
            generated_at=self._clock.now(),
        )
        result = assemble_inference_result(
            skeleton,
            reasoning=None,
            synthesis=None,
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
                for reference in sealed.fragment_allowlist
            ),
            transcript_item_refs=sealed_transcript_item_refs(sealed),
            model_execution_summary=ModelExecutionSummary(
                model=task.task.waterbody_model,
                planning_calls=0,
                tool_calls=len(handoff.tool_result_refs),
                reasoning_batches=0,
                repair_calls=0,
                synthesis_calls=0,
                skipped_cells=skeleton.planned_cell_count,
                usage=None,
            ),
        )
        await transcript_store.write_agent_input(
            task,
            initial_evidence_bundle_ref=handoff.bundle_ref,
        )
        session = await transcript_store.complete_with_result(result)
        if session.status is AgentSessionStatus.CANCELLED:
            dependency_outcome = DependencyOutcome(
                run_id=task.task.run_id,
                target_comid=task.task.target_comid,
                session_id=task.task.session_id,
                status=DependencyStatus.CANCELLED,
                completed_at=session.updated_at,
            )
            committed_result = None
        elif session.status is AgentSessionStatus.COMPLETED:
            if (
                session.result_ref is None
                or not isinstance(
                    session.result_summary,
                    WaterbodySessionResultSummary,
                )
            ):
                raise RuntimeError("completed zero-model session lacks its result")
            dependency_outcome = DependencyOutcome(
                run_id=task.task.run_id,
                target_comid=task.task.target_comid,
                session_id=task.task.session_id,
                status=DependencyStatus.INSUFFICIENT,
                result_ref=session.result_ref,
                result_summary=session.result_summary,
                completed_at=session.updated_at,
            )
            committed_result = result
        else:
            raise RuntimeError("zero-model session did not reach a terminal state")
        return ZeroModelResultOutcome(
            result=committed_result,
            session=session,
            dependency_outcome=dependency_outcome,
        )
