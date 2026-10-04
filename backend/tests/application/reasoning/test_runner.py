import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    SessionResultReference,
    WaterbodySessionResultSummary,
)
from app.application.reasoning import (
    InferenceAssemblyError,
    ModelFitCapability,
    PlanningConvergenceReason,
    ReasoningBatchPlanner,
    ReasoningPlannerLimits,
    RunSingleWaterbodyAgent,
    WaterbodyAgentRunStatus,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    UsageReported,
    WaterbodyFinishReason,
    WaterbodyModelEvent,
    WaterbodyModelPhase,
)
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.domain import AssessmentAvailability, OutcomeStatus
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import (
    WaterbodyModelContentFilteredError,
    WaterbodyModelProtocolError,
    WaterbodyModelTimeoutError,
    WaterbodyModelUnavailableError,
)
from tests.application.evidence_tools.conftest import MODEL
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    complete_batch,
)
from tests.application.reasoning.test_batch_planner import ExactByteEstimator
from tests.application.reasoning.test_planning_loop import _fixture
from tests.application.reasoning.test_task_and_skeleton import _catalog


ERROR_ID = UUID("90000000-0000-4000-8000-000000000001")
RESULT_ITEM_ID = UUID("90000000-0000-4000-8000-000000000002")


class RecordingTranscriptStore:
    def __init__(self) -> None:
        self.inputs = []
        self.results = []
        self.exchanges = []

    async def write_agent_input(
        self,
        task,
        *,
        initial_evidence_bundle_ref=None,
    ) -> None:
        self.inputs.append((task, initial_evidence_bundle_ref))

    async def complete_with_result(self, result):
        self.results.append(result)
        task = self.inputs[-1][0]
        return task.session.model_copy(
            update={
                "status": AgentSessionStatus.COMPLETED,
                "updated_at": result.generated_at,
                "result_ref": SessionResultReference(
                    run_id=task.task.run_id,
                    item_id=RESULT_ITEM_ID,
                ),
                "result_summary": WaterbodySessionResultSummary(
                    result_id=result.result_id,
                    target_comid=result.target_comid,
                    risk_level=result.overall_assessment.risk_level,
                    availability=result.overall_assessment.availability,
                    overall_confidence=result.overall_confidence,
                ),
            }
        )

    async def write_validated_exchange(self, audit) -> None:
        self.exchanges.append(audit)


class AdaptiveWaterbodyModelPort:
    def __init__(
        self,
        *,
        fail_phase: WaterbodyModelPhase | None = None,
        failure_type=WaterbodyModelTimeoutError,
    ) -> None:
        self.fail_phase = fail_phase
        self.failure_type = failure_type
        self.calls = []

    async def stream(self, request) -> AsyncIterator[WaterbodyModelEvent]:
        self.calls.append(request)
        if request.phase is self.fail_phase:
            raise self.failure_type(
                provider=request.model.provider,
                model=request.model.model,
                operation_id=str(request.operation_id),
            )
        payload = self._payload(request)

        values = {"operation_id": request.operation_id, "model": request.model}
        yield StructuredOutputCompleted(
            **values,
            output_id=f"output:{request.operation_id}",
            value=json.dumps(payload, separators=(",", ":")),
        )
        yield UsageReported(
            **values,
            input_tokens=10,
            output_tokens=5,
        )
        yield Terminal(
            **values,
            provider_response_id=f"response:{request.operation_id}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )

    def _payload(self, request):
        if request.phase is WaterbodyModelPhase.EVIDENCE_PLANNING:
            return {"decision": "evidence_ready"}
        if request.phase is WaterbodyModelPhase.REASONING_BATCH:
            return self._reasoning_payload(request)
        if request.phase is WaterbodyModelPhase.OVERALL_SYNTHESIS:
            return self._synthesis_payload(request)
        raise AssertionError("adaptive fake received an unexpected repair")

    @staticmethod
    def _body(request):
        return json.loads(request.messages[-1].text)

    def _reasoning_payload(self, request):
        body = self._body(request)
        summaries = {
            item["evidence_ref"]: item for item in body["evidence_summaries"]
        }
        cells = []
        for planned in body["cells"]:
            refs = planned["allowed_evidence_refs"]
            series_refs = sorted(
                {
                    series_ref
                    for evidence_ref in refs
                    for series_ref in summaries[evidence_ref]["series_refs"]
                }
            )
            source = (
                "inferred"
                if "inferred" in planned["allowed_result_sources"]
                else planned["allowed_result_sources"][0]
            )
            cells.append(
                {
                    "cell_id": planned["cell_id"],
                    "outcome_status": "directional",
                    "representative_value": None,
                    "target_unit_id": None,
                    "source": source,
                    "trend": "rising",
                    "risk_level": "medium",
                    "risk_rationale": "The cited evidence supports a material risk signal.",
                    "confidence": 0.7,
                    "confidence_rationale": "The judgment is limited to sealed evidence.",
                    "aggregation_method": None,
                    "evidence_refs": refs,
                    "series_refs": series_refs,
                    "reason_codes": [],
                    "quality_flags": [],
                    "explanation": "The direction is supported without inventing a value.",
                }
            )
        return {
            "schema_version": 1,
            "decision_type": "reasoning_batch",
            "batch_id": request.batch_id,
            "scope_digest": request.scope_digest,
            "cells": cells,
        }

    def _synthesis_payload(self, request):
        body = self._body(request)
        validated = body["validated_cells"]
        fixed = body["fixed_cells"]
        by_parameter = {}
        by_window = {}
        for cell in validated:
            _, code_system, code, _window = cell["cell_id"].split(":", 3)
            parameter = (code_system, code)
            by_parameter.setdefault(parameter, []).append(cell)
            by_window.setdefault(cell["window_id"], []).append(cell)
        for cell in fixed:
            _, code_system, code, _window = cell["cell_id"].split(":", 3)
            by_parameter.setdefault((code_system, code), [])
            by_window.setdefault(cell["window_id"], [])

        series = []
        for (code_system, code), cells in sorted(by_parameter.items()):
            refs = [cell["cell_id"] for cell in cells]
            evidence = sorted(
                {ref for cell in cells for ref in cell["evidence_refs"]}
            )
            series.append(
                {
                    "parameter": {"code_system": code_system, "code": code},
                    "confidence": 0.7 if refs else 0.0,
                    "confidence_rationale": (
                        "The series uses validated result cells."
                        if refs
                        else "No supported cell is available for this series."
                    ),
                    "contributing_cell_refs": refs,
                    "evidence_refs": evidence,
                }
            )
        windows = []
        for window_id, cells in sorted(by_window.items()):
            refs = [cell["cell_id"] for cell in cells]
            evidence = sorted(
                {ref for cell in cells for ref in cell["evidence_refs"]}
            )
            windows.append(
                {
                    "window_id": window_id,
                    "risk_level": "medium" if refs else "indeterminate",
                    "risk_rationale": (
                        "Validated cells support a material risk signal."
                        if refs
                        else "No supported cell is available for this window."
                    ),
                    "confidence": 0.7 if refs else 0.0,
                    "confidence_rationale": (
                        "The window uses validated result cells."
                        if refs
                        else "No supported water-quality judgment is available."
                    ),
                    "contributing_cell_refs": refs,
                    "evidence_refs": evidence,
                }
            )
        all_cells = [cell["cell_id"] for cell in validated]
        all_evidence = sorted(
            {ref for cell in validated for ref in cell["evidence_refs"]}
        )
        first = validated[0]
        return {
            "schema_version": 1,
            "decision_type": "overall_synthesis",
            "scope_digest": request.scope_digest,
            "series_assessments": series,
            "window_assessments": windows,
            "overall": {
                "risk_level": "medium",
                "risk_rationale": "Validated cells support a material range risk signal.",
                "confidence": 0.7,
                "confidence_rationale": "The range uses every validated result cell.",
                "contributing_window_refs": [
                    f"overall-window:{window_id}" for window_id in sorted(by_window)
                ],
                "contributing_cell_refs": all_cells,
                "evidence_refs": all_evidence,
            },
            "analysis_detail": {
                "summary": "The result follows the sealed evidence and validated cells.",
                "steps": [
                    {
                        "type": "evidence_review",
                        "title": "Review sealed evidence",
                        "content": "Only the cited evidence was used.",
                        "evidence_refs": [first["evidence_refs"][0]],
                        "result_cell_refs": [first["cell_id"]],
                    }
                ],
            },
            "attribution_summary": {
                "summary": "The sealed evidence is the primary factor.",
                "primary_factors": [
                    {
                        "factor": "Sealed water observation",
                        "direction": "increasing",
                        "magnitude": "medium",
                        "basis": "inferred",
                        "explanation": "The factor is limited to the cited result.",
                        "evidence_refs": [first["evidence_refs"][0]],
                        "result_cell_refs": [first["cell_id"]],
                    }
                ],
            },
        }


def _runner(loop, port, *, planner=None, clock=None, transcript_store=None):
    prompt_builder = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY)
    model_fit = ModelFitCapability(
        model=MODEL,
        context_window_tokens=10_000_000,
        max_output_tokens=1_000_000,
    )
    planner = planner or ReasoningBatchPlanner(
            prompt_builder=prompt_builder,
            model_catalog=_catalog(),
            size_estimator=ExactByteEstimator(),
        )
    return RunSingleWaterbodyAgent(
        planning_loop=loop,
        capability_registry=load_parameter_capability_registry(),
        batch_planner=planner,
        prompt_builder=prompt_builder,
        model_port=port,
        model_fit=model_fit,
        result_id_factory=lambda: "result:m8:fixture",
        error_id_factory=lambda: ERROR_ID,
        clock=clock or FixedClock(),
        transcript_store=transcript_store,
    )


def test_runner_executes_planning_batches_synthesis_and_deterministic_assembly():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, (
            outcome.error,
            outcome.audits[-1].initial_issues if outcome.audits else (),
        )
        assert outcome.error is None
        assert outcome.result is not None
        assert outcome.result.result_id == "result:m8:fixture"
        assert all(
            point.outcome_status is OutcomeStatus.DIRECTIONAL
            for series in outcome.result.series
            for point in series.points
        )
        assert outcome.result.overall_assessment.availability is (
            AssessmentAvailability.COMPLETE
        )
        assert outcome.result.model_execution_summary.planning_calls == 1
        assert outcome.result.model_execution_summary.reasoning_batches == 1
        assert outcome.result.model_execution_summary.synthesis_calls == 1
        assert outcome.planning_counts.reasoning_batch_calls == 1
        assert outcome.planning_counts.synthesis_calls == 1
        assert outcome.result.model_execution_summary.usage.input_tokens == 30
        assert [call.phase for call in port.calls] == [
            WaterbodyModelPhase.EVIDENCE_PLANNING,
            WaterbodyModelPhase.REASONING_BATCH,
            WaterbodyModelPhase.OVERALL_SYNTHESIS,
        ]

    asyncio.run(exercise())


def test_runner_reuses_m7_preflight_handoff_without_duplicate_collection() -> None:
    async def exercise() -> None:
        port = AdaptiveWaterbodyModelPort()
        scope, store, loop, task = await _fixture(port, water_mode="full")
        handoff = await loop._acquisition.start(scope)
        preflight_calls = len(store.calls)
        transcript = RecordingTranscriptStore()

        outcome = await _runner(
            loop,
            port,
            transcript_store=transcript,
        ).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
            initial_handoff=handoff,
        )

        assert outcome.status is WaterbodyAgentRunStatus.COMPLETED
        assert len(store.calls) == preflight_calls
        assert outcome.planning_counts is not None
        assert outcome.planning_counts.preflight_tool_calls == len(
            handoff.tool_result_refs
        )
        assert transcript.inputs == [(task, handoff.bundle_ref)]
        assert transcript.results == [outcome.result]

    asyncio.run(exercise())


def test_runner_all_fixed_station_path_skips_batch_and_synthesis():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="empty")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
        assert outcome.result is not None
        assert port.calls == []
        assert outcome.result.model_execution_summary.planning_calls == 0
        assert outcome.result.model_execution_summary.reasoning_batches == 0
        assert outcome.result.model_execution_summary.synthesis_calls == 0
        assert outcome.result.overall_assessment.availability is (
            AssessmentAvailability.INDETERMINATE
        )

    asyncio.run(exercise())


def test_stationless_natural_absence_skips_all_model_reasoning():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(
            port,
            with_station=False,
            with_upstream=True,
            upstream_port=FakeCollectionPort(
                lambda plan: complete_batch(plan)
            ),
            upstream_ready=True,
        )
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
        assert outcome.result is not None
        assert port.calls == []
        assert outcome.result.model_execution_summary.planning_calls == 0
        assert outcome.result.model_execution_summary.reasoning_batches == 0
        assert outcome.result.model_execution_summary.synthesis_calls == 0

    asyncio.run(exercise())


def test_synthesis_failure_keeps_batch_audit_and_never_publishes_partial_result():
    async def exercise():
        port = AdaptiveWaterbodyModelPort(
            fail_phase=WaterbodyModelPhase.OVERALL_SYNTHESIS
        )
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == "waterbody_model_timeout"
        assert outcome.error.failed_stage == "overall_synthesis"
        assert outcome.error.retryable is True
        assert len(outcome.audits) == 1
        assert outcome.audits[0].original_phase is (
            WaterbodyModelPhase.REASONING_BATCH
        )

    asyncio.run(exercise())


def test_cancel_before_runner_start_wins_without_calls_or_result():
    async def exercise():
        event = asyncio.Event()
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(
            port,
            water_mode="full",
            cancel_event=event,
        )
        event.set()
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.CANCELLED
        assert outcome.result is None
        assert outcome.error is None
        assert port.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("failure_type", "expected_code", "retryable"),
    [
        (
            WaterbodyModelUnavailableError,
            "waterbody_model_unavailable",
            True,
        ),
        (
            WaterbodyModelContentFilteredError,
            "waterbody_model_content_filtered",
            False,
        ),
        (
            WaterbodyModelProtocolError,
            "waterbody_model_response_invalid",
            False,
        ),
    ],
)
def test_public_model_error_mapping_is_safe_and_stage_specific(
    failure_type,
    expected_code,
    retryable,
):
    async def exercise():
        port = AdaptiveWaterbodyModelPort(
            fail_phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
            failure_type=failure_type,
        )
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == expected_code
        assert outcome.error.failed_stage == "evidence_planning"
        assert outcome.error.retryable is retryable
        assert set(outcome.error.model_dump()) == {
            "error_id",
            "code",
            "message",
            "failed_stage",
            "retryable",
        }

    asyncio.run(exercise())


def test_invalid_initial_and_failed_repair_map_to_repair_failed_without_result():
    class InvalidPort(AdaptiveWaterbodyModelPort):
        def _reasoning_payload(self, request):
            return {"invalid": True}

    async def exercise():
        port = InvalidPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == "waterbody_model_repair_failed"
        assert outcome.error.failed_stage == "schema_repair"
        assert len(outcome.audits) == 1
        assert outcome.audits[0].repair_request is not None

    asyncio.run(exercise())


def test_successful_repair_is_counted_and_continues_to_synthesis():
    class RepairingPort(AdaptiveWaterbodyModelPort):
        def __init__(self):
            super().__init__()
            self.original_batch_request = None

        def _payload(self, request):
            if request.phase is WaterbodyModelPhase.REASONING_BATCH:
                self.original_batch_request = request
                return {"invalid": True}
            if request.phase is WaterbodyModelPhase.SCHEMA_REPAIR:
                assert self.original_batch_request is not None
                return self._reasoning_payload(self.original_batch_request)
            return super()._payload(request)

    async def exercise():
        port = RepairingPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
        assert outcome.result is not None
        assert outcome.result.model_execution_summary.repair_calls == 1
        assert [call.phase for call in port.calls] == [
            WaterbodyModelPhase.EVIDENCE_PLANNING,
            WaterbodyModelPhase.REASONING_BATCH,
            WaterbodyModelPhase.SCHEMA_REPAIR,
            WaterbodyModelPhase.OVERALL_SYNTHESIS,
        ]

    asyncio.run(exercise())


def test_plan_limit_fails_before_any_reasoning_batch_call():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        prompt_builder = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY)
        planner = ReasoningBatchPlanner(
            prompt_builder=prompt_builder,
            model_catalog=_catalog(),
            size_estimator=ExactByteEstimator(),
            limits=ReasoningPlannerLimits(
                max_cells_per_batch=1,
                max_batches=1,
                max_serialized_input_bytes=10_000_000,
                max_serialized_output_bytes=10_000_000,
            ),
        )
        outcome = await _runner(loop, port, planner=planner).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.error is not None
        assert outcome.error.code == "reasoning_plan_too_large"
        assert outcome.error.failed_stage == "batch_planning"
        assert [call.phase for call in port.calls] == [
            WaterbodyModelPhase.EVIDENCE_PLANNING
        ]

    asyncio.run(exercise())


def test_deadline_at_boundary_fails_without_starting_an_operation():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now(),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == "reasoning_deadline_exceeded"
        assert outcome.error.failed_stage == "evidence_planning"
        assert port.calls == []

    asyncio.run(exercise())


def test_budget_exhaustion_with_all_required_cells_blocked_is_not_natural_empty():
    class BudgetExhaustedLoop:
        def __init__(self, inner):
            self.inner = inner

        async def run(self, task, *, deadline):
            outcome = await self.inner.run(task, deadline=deadline)
            return replace(
                outcome,
                counts=replace(
                    outcome.counts,
                        convergence_reason=(
                            PlanningConvergenceReason.TOOL_BUDGET_EXHAUSTED
                        ),
                ),
            )

    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(
            port,
            with_station=False,
            with_upstream=True,
        )
        outcome = await _runner(BudgetExhaustedLoop(loop), port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == "reasoning_budget_exceeded"
        assert outcome.error.failed_stage == "evidence_planning"

    asyncio.run(exercise())


def test_cancellation_after_a_validated_batch_prevents_synthesis_and_result():
    class BoundaryClock:
        def __init__(self, event):
            self.value = FixedClock().now()
            self.event = event
            self.calls = 0

        def now(self):
            self.calls += 1
            if self.calls == 6:
                self.event.set()
            return self.value

    async def exercise():
        event = asyncio.Event()
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(
            port,
            water_mode="full",
            cancel_event=event,
        )
        outcome = await _runner(
            loop,
            port,
            clock=BoundaryClock(event),
        ).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.CANCELLED
        assert outcome.result is None
        assert outcome.error is None
        assert len(outcome.audits) == 1
        assert [call.phase for call in port.calls] == [
            WaterbodyModelPhase.EVIDENCE_PLANNING,
            WaterbodyModelPhase.REASONING_BATCH,
        ]

    asyncio.run(exercise())


def test_assembly_failure_maps_safely_and_retains_completed_exchange_audits(
    monkeypatch,
):
    def fail_assembly(*_args, **_kwargs):
        raise InferenceAssemblyError()

    async def exercise():
        monkeypatch.setattr(
            "app.application.reasoning.runner.assemble_inference_result",
            fail_assembly,
        )
        port = AdaptiveWaterbodyModelPort()
        _scope, _store, loop, task = await _fixture(port, water_mode="full")
        outcome = await _runner(loop, port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.status is WaterbodyAgentRunStatus.FAILED
        assert outcome.result is None
        assert outcome.error is not None
        assert outcome.error.code == "inference_assembly_failed"
        assert outcome.error.failed_stage == "assembling_result"
        assert len(outcome.audits) == 2

    asyncio.run(exercise())
