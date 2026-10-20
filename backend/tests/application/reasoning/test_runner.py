import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
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
    WaterbodyModelCancelledError,
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
        if request.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION:
            return {
                "value": None,
                "value_lower": None,
                "value_upper": None,
                "confidence": 0.0,
                "rationale": "No useful range can be inferred from the available evidence.",
            }
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
        details = {
            evidence_ref: json.loads(summary["summary"])
            for evidence_ref, summary in summaries.items()
        }
        cells = []
        for planned in body["cells"]:
            allowed_refs = planned["allowed_evidence_refs"]
            target = planned["parameter"]
            numeric_refs = tuple(
                evidence_ref
                for evidence_ref in allowed_refs
                if target in details[evidence_ref]["parameters"]
                and details[evidence_ref]["payload"]["payload_type"]
                in {"aggregate", "temporal_estimate"}
                and details[evidence_ref]["payload"].get("value") is not None
                and details[evidence_ref]["payload"].get("unit")
                in planned["allowed_unit_ids"]
                and details[evidence_ref]["quality_class"]
                in {"valid", "suspicious"}
                and details[evidence_ref]["result_eligibility"] == "inferred_only"
                and (
                    details[evidence_ref]["payload"]["payload_type"]
                    == "temporal_estimate"
                    or details[evidence_ref]["payload"]["method"]
                    in planned["allowed_aggregation_methods"]
                )
            )
            target_ref = next(
                (ref for ref in numeric_refs
                 if details[ref]["payload"]["payload_type"] == "aggregate"),
                numeric_refs[0] if numeric_refs else None,
            )
            support_ref = None
            if target_ref is not None and target != {"code_system": "usgs", "code": "00060"}:
                support_ref = next(
                    (ref for ref in allowed_refs
                     if {"code_system": "usgs", "code": "00060"}
                     in details[ref]["parameters"]
                     and details[ref]["payload"]["payload_type"]
                     in {"aggregate", "temporal_estimate", "direct_record"}
                     and details[ref]["quality_class"] in {"valid", "suspicious"}),
                    None,
                )
            numeric = (
                target_ref is not None
                and "inferred" in planned["allowed_result_sources"]
                and (
                    target == {"code_system": "usgs", "code": "00060"}
                    or support_ref is not None
                )
            )
            refs = sorted({target_ref, support_ref} - {None}) if numeric else allowed_refs
            temporal = tuple(
                details[ref]["payload"] for ref in refs
                if details[ref]["payload"]["payload_type"] == "temporal_estimate"
            )
            window_seconds = (
                datetime.fromisoformat(planned["end_time"])
                - datetime.fromisoformat(planned["start_time"])
            ).total_seconds()
            caps = tuple(
                round(
                    (0.75 if payload["method"] == "linear_interpolation" else 0.55)
                    * min(1.0, window_seconds / max(payload["anchor_distance_seconds"])),
                    6,
                )
                for payload in temporal
            )
            confidence = min((0.7, *caps))
            quality_flags = sorted({
                flag
                for evidence_ref in refs
                for flag in details[evidence_ref]["quality_flags"]
            })
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
            target_payload = details[target_ref]["payload"] if numeric else None
            cells.append(
                {
                    "cell_id": planned["cell_id"],
                    "outcome_status": "quantified" if numeric else "directional",
                    "representative_value": target_payload["value"] if numeric else None,
                    "target_unit_id": target_payload["unit"] if numeric else None,
                    "source": source,
                    "trend": "rising",
                    "risk_level": "medium",
                    "risk_rationale": "The cited evidence supports a material risk signal.",
                    "confidence": confidence,
                    "confidence_rationale": (
                        f"Validated {temporal[0]['method']} estimate; the farthest "
                        f"anchor is {max(temporal[0]['anchor_distance_seconds'])} "
                        "seconds away, limiting confidence."
                        if temporal else "The judgment is limited to sealed evidence."
                    ),
                    "aggregation_method": (
                        target_payload["method"]
                        if numeric and target_payload["payload_type"] == "aggregate"
                        else None
                    ),
                    "evidence_refs": refs,
                    "series_refs": series_refs,
                    "reason_codes": [],
                    "quality_flags": quality_flags,
                    "explanation": (
                        "The cited deterministic evidence supports this inferred value."
                        if numeric else "The direction is supported without inventing a value."
                    ),
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
        details = {
            item["evidence_ref"]: json.loads(item["summary"])
            for item in body["bounded_reference_summaries"]
        }
        attribution = next(
            (
                (cell, evidence_ref)
                for cell in validated
                for evidence_ref in cell["evidence_refs"]
                if (detail := details.get(evidence_ref))
                and detail["applicability"] == "direct_measurement"
                and detail["payload"]["payload_type"]
                in {"direct_record", "aggregate"}
                and {
                    "code_system": cell["cell_id"].split(":", 3)[1],
                    "code": cell["cell_id"].split(":", 3)[2],
                } in detail["parameters"]
            ),
            None,
        )
        primary_factors = []
        if attribution is not None:
            cell, evidence_ref = attribution
            primary_factors.append(
                {
                    "factor": "Cited water evidence",
                    "direction": "increasing",
                    "magnitude": "medium",
                    "basis": "inferred",
                    "explanation": "The factor is limited to the cited result.",
                    "evidence_refs": [evidence_ref],
                    "result_cell_refs": [cell["cell_id"]],
                }
            )
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
                "summary": (
                    "The cited measured-window evidence supports the factor."
                    if primary_factors
                    else "No supported attribution is available from these cells."
                ),
                "primary_factors": primary_factors,
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


@pytest.mark.parametrize(
    ("target_payload", "flow_payload", "expected_method", "expected_confidence"),
    (
        (
            {"payload_type": "aggregate", "value": "8.1", "unit": "pH", "method": "latest"},
            {"payload_type": "temporal_estimate", "value": "20", "unit": "ft3/s",
             "method": "nearest_hold", "anchor_distance_seconds": [43200]},
            "latest",
            0.275,
        ),
        (
            {"payload_type": "temporal_estimate", "value": "8.2", "unit": "pH",
             "method": "linear_interpolation", "anchor_distance_seconds": [21600, 64800]},
            {"payload_type": "aggregate", "value": "20", "unit": "ft3/s", "method": "mean"},
            None,
            0.25,
        ),
    ),
)
def test_adaptive_fake_quantifies_registered_values_with_temporal_confidence_cap(
    target_payload,
    flow_payload,
    expected_method,
    expected_confidence,
) -> None:
    target = {"code_system": "usgs", "code": "00400"}
    flow = {"code_system": "usgs", "code": "00060"}

    def summary(evidence_ref, parameter, payload):
        return {
            "evidence_ref": evidence_ref,
            "series_refs": [f"series:{evidence_ref}"],
            "summary": json.dumps({
                "data_type": payload["payload_type"],
                "payload": payload,
                "parameters": [parameter],
                "quality_class": "suspicious",
                "quality_flags": (
                    ["temporal_estimate"]
                    if payload["payload_type"] == "temporal_estimate" else []
                ),
                "result_eligibility": "inferred_only",
            }),
        }

    body = {
        "cells": [{
            "cell_id": "cell:usgs:00400:window-1",
            "parameter": target,
            "start_time": "2026-10-02T00:00:00+00:00",
            "end_time": "2026-10-02T06:00:00+00:00",
            "allowed_evidence_refs": ["e:target", "e:flow"],
            "allowed_result_sources": ["inferred"],
            "allowed_unit_ids": ["pH"],
            "allowed_aggregation_methods": ["latest"],
        }],
        "evidence_summaries": [
            summary("e:target", target, target_payload),
            summary("e:flow", flow, flow_payload),
        ],
    }
    request = SimpleNamespace(
        messages=(SimpleNamespace(text=json.dumps(body)),),
        batch_id="batch-1",
        scope_digest="digest-1",
    )
    cell = AdaptiveWaterbodyModelPort()._reasoning_payload(request)["cells"][0]
    assert cell["outcome_status"] == "quantified"
    assert cell["representative_value"] == target_payload["value"]
    assert cell["target_unit_id"] == target_payload["unit"]
    assert cell["source"] == "inferred"
    assert cell["aggregation_method"] == expected_method
    assert cell["evidence_refs"] == ["e:flow", "e:target"]
    assert cell["quality_flags"] == ["temporal_estimate"]
    assert cell["confidence"] == expected_confidence
    assert "anchor" in cell["confidence_rationale"]

    body["cells"][0]["allowed_result_sources"] = ["observed"]
    request.messages[0].text = json.dumps(body)
    unpermitted = AdaptiveWaterbodyModelPort()._reasoning_payload(request)["cells"][0]
    assert unpermitted["outcome_status"] == "directional"


def test_adaptive_fake_synthesis_uses_a_measured_window_for_attribution() -> None:
    def summary(evidence_ref, parameter, payload_type):
        return {
            "evidence_ref": evidence_ref,
            "summary": json.dumps({
                "applicability": (
                    "allowed_indirect"
                    if payload_type == "temporal_estimate" else "direct_measurement"
                ),
                "parameters": [{"code_system": "usgs", "code": parameter}],
                "payload": {"payload_type": payload_type},
            }),
        }

    estimated = {
        "cell_id": "cell:usgs:00400:window-1",
        "window_id": "window-1",
        "evidence_refs": ["e:flow-estimate", "e:ph-estimate"],
    }
    measured = {
        "cell_id": "cell:usgs:00400:window-2",
        "window_id": "window-2",
        "evidence_refs": ["e:flow-aggregate", "e:ph-aggregate"],
    }
    body = {
        "validated_cells": [estimated, measured],
        "fixed_cells": [],
        "bounded_reference_summaries": [
            summary("e:flow-estimate", "00060", "temporal_estimate"),
            summary("e:ph-estimate", "00400", "temporal_estimate"),
            summary("e:ph-anchor", "00400", "aggregate"),
            summary("e:flow-aggregate", "00060", "aggregate"),
            summary("e:ph-aggregate", "00400", "aggregate"),
        ],
    }
    request = SimpleNamespace(
        messages=(SimpleNamespace(text=json.dumps(body)),),
        scope_digest="digest-1",
    )
    port = AdaptiveWaterbodyModelPort()
    factor = port._synthesis_payload(request)["attribution_summary"]["primary_factors"]
    assert factor == [{
        "factor": "Cited water evidence",
        "direction": "increasing",
        "magnitude": "medium",
        "basis": "inferred",
        "explanation": "The factor is limited to the cited result.",
        "evidence_refs": ["e:ph-aggregate"],
        "result_cell_refs": [measured["cell_id"]],
    }]

    body["validated_cells"] = [estimated]
    request.messages[0].text = json.dumps(body)
    assert port._synthesis_payload(request)["attribution_summary"][
        "primary_factors"
    ] == []


def test_runner_executes_planning_batches_synthesis_and_deterministic_assembly():
    async def exercise():
        port = AdaptiveWaterbodyModelPort()
        _scope, store, loop, task = await _fixture(port, water_mode="full")
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
        sealed_evidence_ids = {
            reference.evidence_id
            for reference in outcome.result.evidence_chain.references
        }
        assert outcome.result.analysis_detail.transcript_item_refs == tuple(
            sorted(
                {
                    *(
                        item_id for item_id, evidence_id in store.fragments
                        if evidence_id in sealed_evidence_ids
                    ),
                    *(item_id for _run_id, _session_id, item_id in store.records),
                }
            )
        )
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
        for call in port.calls:
            body = json.loads(call.messages[-1].text)
            summaries = body.get(
                "evidence_summaries", body.get("bounded_reference_summaries", ())
            )
            assert any(
                any(
                    record.get("value") == "7" and record.get("raw_unit") == "mg/l"
                    for record in json.loads(item["summary"])["records"]
                )
                for item in summaries
            ), call.phase

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
        assert outcome.planning_counts is not None
        assert outcome.planning_counts.numeric_estimation_calls > 0
        assert {call.phase for call in port.calls} == {
            WaterbodyModelPhase.EVIDENCE_PLANNING,
            WaterbodyModelPhase.NUMERIC_ESTIMATION,
        }
        assert outcome.result.model_execution_summary.planning_calls == 1
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
        assert outcome.planning_counts is not None
        assert outcome.planning_counts.numeric_estimation_calls > 0
        assert {call.phase for call in port.calls} == {
            WaterbodyModelPhase.EVIDENCE_PLANNING,
            WaterbodyModelPhase.NUMERIC_ESTIMATION,
        }
        assert outcome.result.model_execution_summary.planning_calls == 1
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
            WaterbodyModelCancelledError,
            "waterbody_model_cancelled",
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
