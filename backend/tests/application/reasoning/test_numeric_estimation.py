import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.workflow import WorkflowStopped
from app.application.evidence_tools.numeric_estimation import (
    NumericEstimateRequest,
    NumericEstimationError,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    UsageReported,
    WaterbodyFinishReason,
    WaterbodyModelPhase,
)
from app.application.reasoning.numeric_estimation import (
    ModelNumericEstimator,
    NumericEstimateResponse,
    _fragment_projection,
)
from app.application.reasoning.transcript_store import ReasoningTranscriptStore
from app.application.transcripts.models import TranscriptContentRef
from app.domain import (
    EvidenceFragmentReference,
    ItemReference,
    NumericPlanTask,
    ParameterKey,
    SourceRecordReference,
)
from app.domain.evidence.measured_anchor import MeasuredAnchor
from app.domain.evidence.upstream_result_payload import UpstreamResultPayload
from app.infrastructure.config import load_parameter_capability_registry
from app.ports.errors import (
    WaterbodyModelProtocolError,
    WaterbodyModelUnavailableError,
)
from tests.application.evidence_tools.conftest import MODEL, make_scope
from tests.application.evidence_tools.fakes import FixedClock


class EstimatePort:
    def __init__(self, **changes):
        self.calls = []
        self.payload = {
            "value": None,
            "value_lower": "6.2",
            "value_upper": "8.8",
            "confidence": 0.2,
            "rationale": "A low-basis range from general knowledge; no local measurements are available.",
            **changes,
        }

    async def stream(self, request):
        self.calls.append(request)
        values = {"operation_id": request.operation_id, "model": request.model}
        yield StructuredOutputCompleted(**values, output_id="estimate", value=json.dumps(self.payload))
        yield UsageReported(**values, input_tokens=17, output_tokens=23)
        yield Terminal(**values, provider_response_id="estimate-response", finish_reason=WaterbodyFinishReason.COMPLETED)


def _request(*, anchored=False, cancel_event=None):
    scope = make_scope(with_station=False, cancel_event=cancel_event)
    parameter = ParameterKey(code_system="usgs", code="00400")
    task = NumericPlanTask(
        task_id="estimate-ph",
        target_comid=scope.agent_comid,
        output_parameter=parameter,
        output_unit="standard_units",
        window_id=scope.time_windows[0].window_id,
        mode=scope.mode.value,
        method_id="model_estimate_usgs_00400",
        method_version=1,
        method_kind="model_estimate",
        inputs=(),
    )
    anchors = (MeasuredAnchor(
        parameter=parameter,
        source_record_refs=(SourceRecordReference(item_id="original-records", record_id="ph-1"),),
        origin_comid=scope.agent_comid,
        evidence_time_range=scope.time_windows[0].time_range,
        path_comids=(scope.agent_comid,),
    ),) if anchored else ()
    return NumericEstimateRequest(
        scope=scope, task=task, plan_ref=ItemReference(item_id="saved-plan"),
        input_refs=(), input_fragments=(), context_refs=(), context_fragments=(),
        measured_anchors=anchors,
    )


def _estimator(port, *, bundle_ref=None, **kwargs):
    clock = FixedClock()
    return ModelNumericEstimator(
        model_port=port, model=MODEL,
        bundle_ref=bundle_ref or ItemReference(item_id="seed-bundle"),
        capability_registry=load_parameter_capability_registry(),
        deadline=clock.now() + timedelta(minutes=5), clock=clock, **kwargs,
    )


def test_unanchored_model_uses_shared_phase_records_plan_and_counts_usage():
    async def run():
        port = EstimatePort()
        estimate = _estimator(port)
        task = _request()
        task = replace(task, input_refs=(EvidenceFragmentReference(item_id="durable-input", evidence_id="flow-estimate"),))
        result = await estimate(task)
        assert result.value is None
        assert (result.value_lower, result.value_upper) == ("6.2", "8.8")
        request = port.calls[0]
        assert request.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION
        assert request.numeric_plan_ref == task.plan_ref
        assert request.numeric_input_refs == task.input_refs
        payload = json.loads(request.messages[0].text)
        assert payload["same_parameter_original_measurements_available"] is False
        assert payload["target_location"] == task.scope.target_node.point.model_dump(mode="json")
        assert payload["topology_coverage_facts"] == []
        assert estimate.calls == 1
        assert estimate.usage_reported
        assert (estimate.input_tokens, estimate.output_tokens) == (17, 23)
    asyncio.run(run())


def test_numeric_prompt_includes_matching_saved_topology_gap_only() -> None:
    async def run():
        port = EstimatePort()
        request = _request()
        target_parameter = request.scope.requested_parameters[0]
        task = NumericPlanTask.model_validate({
            **request.task.model_dump(mode="json"),
            "output_parameter": target_parameter.model_dump(mode="json"),
        })
        await _estimator(port)(replace(request, task=task))

        payload = json.loads(port.calls[0].messages[0].text)
        assert len(payload["topology_coverage_facts"]) == 1
        fact = payload["topology_coverage_facts"][0]
        assert f"{target_parameter.code_system}:{target_parameter.code}" in fact
        assert "natural headwater without a qualifying source" in fact
        assert "not a zero reading" in fact
        assert request.scope.topology_ref.item_id in fact

    asyncio.run(run())


def test_numeric_request_records_with_its_plan_and_input_references():
    async def run():
        port = EstimatePort()
        estimator = _estimator(port, bundle_ref=ItemReference(item_id=str(uuid4())))
        task = replace(
            _request(),
            plan_ref=ItemReference(item_id=str(uuid4())),
            input_refs=(EvidenceFragmentReference(
                item_id=str(uuid4()), evidence_id="recorded-flow"
            ),),
        )
        await estimator(task)

        store = ReasoningTranscriptStore(
            operations=object(), run_id=uuid4(), session_id=uuid4()
        )
        saved = []

        async def capture(content_type, body, *, operation_id=None):
            saved.append((content_type, body))
            return TranscriptContentRef(run_id=store.run_id, item_id=uuid4())

        store._write = capture
        await store.write_model_request(port.calls[0])
        assert saved[0][0] == "waterbody_model_request"
        assert saved[0][1].numeric_plan_ref is not None
        assert saved[0][1].numeric_input_refs == task.input_refs

    asyncio.run(run())


def test_point_requires_original_same_parameter_measurements():
    async def run():
        port = EstimatePort(value="7.4")
        estimate = _estimator(port)
        with pytest.raises(ValueError, match="original same-parameter"):
            await estimate(_request())
        result = await _estimator(port)(_request(anchored=True))
        assert result.value == "7.4"
        anchored_request = port.calls[-1]
        assert json.loads(anchored_request.messages[0].text)[
            "same_parameter_original_measurements_available"
        ] is True
        assert "normally give a defensible point prediction" in anchored_request.system_prompt
        assert "ordinary provider_flagged" in anchored_request.system_prompt
    asyncio.run(run())


def test_json_object_response_uses_valid_estimate_fields_despite_extra_context():
    async def run():
        port = EstimatePort(
            type="json_object", task_id="scheduled-task",
            outcome="range_estimate", evidence_refs=["context-fragment"],
        )
        result = await _estimator(port)(_request())
        assert result.value is None
        assert (result.value_lower, result.value_upper) == ("6.2", "8.8")
        assert len(port.calls) == 1

        abstaining = EstimatePort(
            value_lower=None, value_upper=None,
            type="json_object", outcome="indeterminate", risk_level="indeterminate",
        )
        abstention = await _estimator(abstaining)(_request())
        assert abstention.value is None
        assert abstention.value_lower is None and abstention.value_upper is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("label", "expected"),
    [("low", 0.25), ("MEDIUM", 0.5), ("very high", 0.9), ("0.4", 0.4)],
)
def test_verbal_or_numeric_string_confidence_is_normalized(label, expected):
    async def run():
        port = EstimatePort(confidence=label)
        result = await _estimator(port)(_request())
        assert result.confidence == expected
        assert len(port.calls) == 1

    asyncio.run(run())


def test_unknown_confidence_still_fails_validation():
    async def run():
        with pytest.raises(ValidationError):
            await _estimator(EstimatePort(confidence="unsure"))(_request())

    asyncio.run(run())


def test_upstream_evidence_prompt_omits_durable_closure_references():
    payload = UpstreamResultPayload(
        upstream_session_id="upstream-session",
        result_ref=ItemReference(item_id="result"),
        closure_refs=tuple(ItemReference(item_id=f"evidence-{index}") for index in range(500)),
    )
    projection = _fragment_projection(SimpleNamespace(
        evidence_id="upstream-result",
        parameter_refs=(ParameterKey(code_system="usgs", code="00400"),),
        location=SimpleNamespace(model_dump=lambda **_: {"comid": "upstream"}),
        time_range=SimpleNamespace(model_dump=lambda **_: {"start": "saved"}),
        collection_status=SimpleNamespace(value="available"),
        quality_flags=(),
        payload=payload,
    ))
    assert projection["payload"]["result_ref"] == {"item_id": "result"}
    assert "closure_refs" not in projection["payload"]


def test_full_ph_domain_is_abstention_not_prediction():
    async def run():
        estimate = _estimator(EstimatePort(value_lower="0", value_upper="14"))
        result = await estimate(_request())
        assert result.value is result.value_lower is result.value_upper is None
        assert "not a useful prediction" in result.rationale
    asyncio.run(run())


def test_budget_exhaustion_and_cancellation_prevent_extra_model_calls():
    async def run():
        port = EstimatePort()
        estimate = _estimator(port, max_calls=1)
        await estimate(_request())
        with pytest.raises(ValueError, match="call_budget"):
            await estimate(_request())
        assert len(port.calls) == 1
        assert estimate.budget_exhausted
        cancellation = asyncio.Event()
        cancellation.set()
        task = _request(cancel_event=cancellation)
        with pytest.raises(WorkflowStopped):
            await _estimator(port)(task)
        assert len(port.calls) == 1
    asyncio.run(run())


def test_default_budget_caps_concurrent_calls_at_64():
    async def run():
        port = EstimatePort()
        estimator = _estimator(port)
        results = await asyncio.gather(
            *(estimator(_request()) for _ in range(70)), return_exceptions=True
        )
        assert estimator.calls == len(port.calls) == 64
        failures = [result for result in results if isinstance(result, NumericEstimationError)]
        assert len(failures) == 6
        assert {failure.code for failure in failures} == {"numeric_estimation_call_budget_exceeded"}
    asyncio.run(run())


def test_protocol_failure_stops_queued_numeric_model_calls():
    class BrokenPort(EstimatePort):
        async def stream(self, request):
            self.calls.append(request)
            await asyncio.sleep(0)
            yield StructuredOutputCompleted(
                operation_id=uuid4(), model=request.model,
                output_id="estimate", value=json.dumps(self.payload),
            )

    async def run():
        port = BrokenPort()
        estimator = _estimator(port)
        results = await asyncio.gather(
            *(estimator(_request()) for _ in range(8)), return_exceptions=True
        )
        assert len(port.calls) == estimator.calls == 1
        assert sum(isinstance(result, WaterbodyModelProtocolError) for result in results) == 1
        assert [result.code for result in results if isinstance(result, NumericEstimationError)] == [
            "numeric_estimation_model_circuit_open"
        ] * 7

    asyncio.run(run())


def test_invalid_structured_estimate_stops_following_model_calls():
    async def run():
        port = EstimatePort(value_lower=6)
        estimator = _estimator(port)
        with pytest.raises(ValidationError):
            await estimator(_request())
        with pytest.raises(NumericEstimationError, match="model_circuit_open"):
            await estimator(_request())
        assert len(port.calls) == estimator.calls == 1

    asyncio.run(run())


def test_provider_unavailable_stops_queued_numeric_model_calls():
    class UnavailablePort(EstimatePort):
        async def stream(self, request):
            self.calls.append(request)
            await asyncio.sleep(0)
            raise WaterbodyModelUnavailableError(
                provider=request.model.provider,
                model=request.model.model,
                operation_id=str(request.operation_id),
            )
            yield  # pragma: no cover

    async def run():
        port = UnavailablePort()
        estimator = _estimator(port)
        results = await asyncio.gather(
            *(estimator(_request()) for _ in range(8)), return_exceptions=True
        )
        assert len(port.calls) == estimator.calls == 1
        assert sum(isinstance(result, WaterbodyModelUnavailableError) for result in results) == 1
        assert sum(
            isinstance(result, NumericEstimationError)
            and result.code == "numeric_estimation_model_circuit_open"
            for result in results
        ) == 7

    asyncio.run(run())


def test_expired_deadline_does_not_dispatch_and_inflight_timeout_is_typed():
    class SlowPort(EstimatePort):
        async def stream(self, request):
            self.calls.append(request)
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async def run():
        clock = FixedClock()
        port = SlowPort()
        common = {
            "model_port": port, "model": MODEL, "bundle_ref": ItemReference(item_id="bundle"),
            "capability_registry": load_parameter_capability_registry(), "clock": clock,
        }
        expired = ModelNumericEstimator(**common, deadline=clock.now())
        with pytest.raises(NumericEstimationError, match="deadline_exceeded"):
            await expired(_request())
        assert expired.calls == len(port.calls) == 0
        active = ModelNumericEstimator(**common, deadline=clock.now() + timedelta(milliseconds=10))
        with pytest.raises(NumericEstimationError, match="deadline_exceeded"):
            await active(_request())
        assert active.calls == len(port.calls) == 1
    asyncio.run(run())


def test_estimation_budget_failure_reaches_planning_and_result_summary():
    from app.application.reasoning.planning_loop import PlanningLoopLimits
    from tests.application.reasoning.test_planning_loop import _fixture
    from tests.application.reasoning.test_runner import (
        AdaptiveWaterbodyModelPort,
        _runner,
    )

    class AdaptiveWithEstimates(AdaptiveWaterbodyModelPort):
        def _payload(self, request):
            if request.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION:
                return {"value": None, "value_lower": None, "value_upper": None,
                        "confidence": 0.0, "rationale": "No useful flow estimate."}
            return super()._payload(request)

    async def run():
        port = AdaptiveWithEstimates()
        _, _, loop, task = await _fixture(
            port, water_mode="partial", limits=PlanningLoopLimits(max_numeric_estimation_calls=1)
        )
        outcome = await _runner(loop, port).run(task, deadline=FixedClock().now() + timedelta(minutes=5))
        assert outcome.result is not None, outcome.error
        assert outcome.planning_counts.numeric_estimation_calls == 1
        assert "numeric_estimation_call_budget_exceeded" in outcome.planning_counts.numeric_failure_codes
        summary = outcome.result.model_execution_summary
        assert summary.numeric_estimation_calls == 1
        assert "numeric_estimation_call_budget_exceeded" in summary.numeric_failure_codes
        assert sum(call.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION for call in port.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"value_lower": 6},
    {"value_lower": "NaN"},
    {"value_lower": "9", "value_upper": "8"},
    {"value": "9"},
    {"value_lower": None},
])
def test_estimate_response_rejects_invalid_numbers(changes):
    with pytest.raises(ValidationError):
        NumericEstimateResponse.model_validate(EstimatePort(**changes).payload)
