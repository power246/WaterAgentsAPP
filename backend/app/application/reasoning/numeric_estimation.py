"""Model execution for a task in the shared numeric dependency schedule."""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from app.application.evidence_tools.numeric_estimation import (
    NumericEstimateRequest,
    NumericEstimateResult,
    NumericEstimationError,
)
from app.application.guidelines.citation_resolver import GuidelineRefs, sanitize_guideline_refs
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    ToolCallCompleted,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.reasoning.prompt_policy import (
    DEFAULT_REASONING_PROMPT_POLICY,
    canonical_json_digest,
    canonical_json_text,
    render_numeric_estimation_system_prompt,
)
from app.application.reasoning.topology_coverage import topology_coverage_facts
from app.domain import (
    DomainModel,
    ItemReference,
    ModelSelection,
    ParameterCapabilityRegistry,
)
from app.domain.common import CanonicalDecimal, NonEmptyString
from app.domain.guidelines import GuidelineUsageTarget
from app.ports.errors import (
    WaterbodyModelCancelledError,
    WaterbodyModelIncompleteError,
    WaterbodyModelProtocolError,
    WaterbodyModelTimeoutError,
    WaterbodyModelTruncatedError,
    WaterbodyModelUnavailableError,
)
from app.ports.waterbody_model import WaterbodyModelPort

_POLICY = """Complete one numeric task in the existing dependency schedule.
Use the supplied parameter, canonical unit, selected time window, durable inputs,
river-network context, related readings of other parameters and relevant knowledge.
Explicit calculations have priority and are handled by the scheduler before this task.
Your output is a model estimate, never a new measurement or a verified formula.
With a traceable original measurement of THIS parameter, normally give a
defensible point prediction and plausible range. Use range-only only when
specific supplied counterevidence makes a point indefensible; name that evidence
and explain why. Lack of a local station or flow, ordinary provider_flagged,
long transfer distance, or usual pH chemistry uncertainty alone is not such
counterevidence; reflect these limits in the range and confidence instead.
Without an original measurement, give only a useful range, or abstain. An upstream
estimate without original measurements remains range-only however often propagated.
General knowledge can support a low-basis range when local information is unavailable;
say explicitly that local information is missing and explain the assumption.
Never substitute a regulatory standard or the entire physical domain for a prediction.
Do not invent local geology, discharges, weather, gauges or absence of disturbances.
Missing flow does not prohibit estimating another parameter. Use available estimated
flow when relevant; explain its absence otherwise. Known confluences, discharges or
reservoirs with unknown effects may widen the range and lower subjective confidence,
but distance or a low confidence score alone must not suppress a useful prediction.
Assess the complete path once; do not multiply confidence penalties at every hop.
An upstream branch without a qualifying source is not a zero reading. Consider
its coverage gap once, without a fixed penalty or automatic abstention.
Confidence is an uncalibrated assessment, not a probability of numerical accuracy.
pH is logarithmic: copying, averaging or flow-weighting upstream pH is not a validated
chemical mixing method. Consider missing buffering and chemistry information.
Return exactly one JSON object with these five required top-level keys:
value, value_lower, value_upper, confidence, rationale. The only optional key is
guideline_refs: null or an array of supplied aliases actually used. Task identifiers, parameter,
outcome, risk, and evidence references are INPUT context, not output fields.
Write confidence as a JSON number from 0 to 1, not a word such as "low".
All numerical values except confidence
must be decimal STRINGS. A point requires lower and upper values around it. Range-only
means value=null. If no useful estimate is possible, all three values must be null and
rationale must explain why. Do not output a whole-domain range such as pH 0 to 14.
"""


class NumericEstimateResponse(DomainModel):
    guideline_refs: GuidelineRefs = Field(default=None, exclude=True)
    value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None
    value_upper: CanonicalDecimal | None
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    rationale: Annotated[NonEmptyString, Field(max_length=4000)]

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value):
        # Some JSON-object providers return a verbal assessment despite the
        # numeric schema. These are display scores, not calibrated probabilities.
        if isinstance(value, str):
            labels = {
                "very low": 0.1,
                "low": 0.25,
                "medium": 0.5,
                "high": 0.75,
                "very high": 0.9,
            }
            normalized = value.strip().lower()
            if normalized in labels:
                return labels[normalized]
            try:
                return float(normalized)
            except ValueError:
                return value
        return value

    @field_validator("value", "value_lower", "value_upper", mode="before")
    @classmethod
    def decimal_strings_only(cls, value):
        if value is not None and not isinstance(value, str):
            raise ValueError("numeric estimates must use decimal strings")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.value_lower is None) != (self.value_upper is None):
            raise ValueError("estimate range requires both bounds")
        if self.value is not None and self.value_lower is None:
            raise ValueError("point estimate requires a possible range")
        if self.value_lower is not None:
            lower, upper = Decimal(self.value_lower), Decimal(self.value_upper)
            if lower >= upper:
                raise ValueError("possible range must have positive width")
            if self.value is not None and not lower <= Decimal(self.value) <= upper:
                raise ValueError("point estimate must lie within the possible range")
        return self


def numeric_estimate_response_schema() -> WaterbodyJsonSchema:
    return WaterbodyJsonSchema(
        name="numeric_estimate",
        schema_text=canonical_json_text(NumericEstimateResponse.model_json_schema()),
    )


class ModelNumericEstimator:
    """Use the session's recorded model port with one bounded call per task."""

    def __init__(
        self,
        *,
        model_port: WaterbodyModelPort,
        model: ModelSelection,
        bundle_ref: ItemReference,
        capability_registry: ParameterCapabilityRegistry,
        deadline: datetime,
        clock,
        max_calls: int = 64,
        operation_id_factory: Callable[[], UUID] = uuid4,
        guidelines=None,
    ) -> None:
        if type(max_calls) is not int or not 1 <= max_calls <= 64:
            raise ValueError("numeric model call limit must be between 1 and 64")
        self._port = model_port
        self._model = model
        self._bundle_ref = bundle_ref
        self._capabilities = capability_registry
        self._deadline = deadline
        self._clock = clock
        self._max_calls = max_calls
        self._operation_id_factory = operation_id_factory
        self._guidelines = guidelines
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_reported = False
        self.budget_exhausted = False
        # The executor can schedule many estimates at once. Keep the first
        # failed provider/response from causing a burst of identical calls.
        self._dispatch_lock = asyncio.Lock()
        self._circuit_open = False

    async def __call__(self, task_request: NumericEstimateRequest) -> NumericEstimateResult:
        scope, task = task_request.scope, task_request.task
        scope.cancel_token.checkpoint()
        if self._circuit_open:
            raise NumericEstimationError("numeric_estimation_model_circuit_open")
        if self.calls >= self._max_calls:
            self.budget_exhausted = True
            raise NumericEstimationError("numeric_estimation_call_budget_exceeded")
        remaining_seconds = (self._deadline - self._clock.now()).total_seconds()
        if remaining_seconds <= 0:
            raise NumericEstimationError("numeric_estimation_deadline_exceeded")
        operation_id = self._operation_id_factory()
        capability = self._capabilities.get(task.output_parameter)
        if capability is None:
            raise ValueError("numeric estimation parameter is unavailable")
        window = next(window for window in scope.time_windows if window.window_id == task.window_id)
        input_by_id = {
            fragment.evidence_id: fragment
            for fragment in (*task_request.input_fragments, *task_request.context_fragments)
        }
        reference_by_id = {
            reference.evidence_id: reference
            for reference in (*task_request.input_refs, *task_request.context_refs)
        }
        payload = {
            "task": task.model_dump(mode="json"),
            "parameter_name": capability.canonical_name,
            "target_location": scope.target_node.point.model_dump(mode="json"),
            "selected_time_range": scope.query_envelope.model_dump(mode="json"),
            "window": window.time_range.model_dump(mode="json"),
            "analysis_timezone": scope.analysis_timezone,
            "same_parameter_original_measurements_available": bool(task_request.measured_anchors),
            "measured_anchors": [
                anchor.model_dump(mode="json", exclude={"origin_point"})
                for anchor in task_request.measured_anchors
            ],
            "topology_coverage_facts": [
                fact.summary for fact in topology_coverage_facts(
                    scope,
                    parameter=task.output_parameter,
                    window_id=task.window_id,
                )
            ],
            "evidence": [_fragment_projection(fragment) for fragment in input_by_id.values()],
            "network_edges": [edge.model_dump(mode="json") for edge in scope.topology.edges],
        }
        message = canonical_json_text(payload)
        if len(message.encode("utf-8")) > 192_000:
            raise NumericEstimationError("numeric_estimation_context_budget_exceeded")
        system_prompt = (render_numeric_estimation_system_prompt() + "\n\n" + _POLICY).strip()
        prompt_digest = canonical_json_digest({"policy": system_prompt, "input": payload})
        request = WaterbodyModelRequest(
            operation_id=operation_id,
            phase=WaterbodyModelPhase.NUMERIC_ESTIMATION,
            model=self._model,
            system_prompt=system_prompt,
            messages=(WaterbodyModelMessage(
                message_id=f"numeric-input:{task.task_id}",
                role=WaterbodyMessageRole.USER,
                text=message,
            ),),
            response_schema=numeric_estimate_response_schema(),
            prompt_policy_digest=DEFAULT_REASONING_PROMPT_POLICY.policy_digest,
            scope_digest=prompt_digest,
            evidence_bundle_ref=self._bundle_ref,
            numeric_plan_ref=task_request.plan_ref,
            numeric_input_refs=tuple(reference_by_id.values()),
            batch_id=task.task_id,
            deadline=self._deadline,
        )
        async with self._dispatch_lock:
            scope.cancel_token.checkpoint()
            if self._circuit_open:
                raise NumericEstimationError("numeric_estimation_model_circuit_open")
            if self.calls >= self._max_calls:
                self.budget_exhausted = True
                raise NumericEstimationError("numeric_estimation_call_budget_exceeded")
            remaining_seconds = (self._deadline - self._clock.now()).total_seconds()
            if remaining_seconds <= 0:
                raise NumericEstimationError("numeric_estimation_deadline_exceeded")
            if self._guidelines is not None:
                request = await self._guidelines.prepare(request, cancellation=scope.cancel_token,
                    parameters=(task.output_parameter,), targets=(task.task_id,), model_port=self._port)
                prompt_digest = canonical_json_digest({"policy": request.system_prompt,
                    "input": [item.model_dump(mode="json") for item in request.messages]})
                remaining_seconds = (self._deadline - self._clock.now()).total_seconds()
                if remaining_seconds <= 0:
                    raise NumericEstimationError("numeric_estimation_deadline_exceeded")
            self.calls += 1
            timeout = asyncio.timeout(remaining_seconds)
            try:
                async with timeout:
                    completed = await consume_waterbody_model_stream(
                        self._port, request, cancellation=scope.cancel_token
                    )
            except (TimeoutError, WaterbodyModelCancelledError):
                if timeout.expired():
                    self._circuit_open = True
                    raise NumericEstimationError("numeric_estimation_deadline_exceeded") from None
                raise
            except (
                WaterbodyModelUnavailableError,
                WaterbodyModelTimeoutError,
                WaterbodyModelProtocolError,
                WaterbodyModelTruncatedError,
                WaterbodyModelIncompleteError,
            ):
                self._circuit_open = True
                raise
            if completed.usage is not None:
                self.usage_reported = True
                self.input_tokens += completed.usage.input_tokens or 0
                self.output_tokens += completed.usage.output_tokens or 0
            try:
                outputs = tuple(
                    event for event in completed.events
                    if isinstance(event, StructuredOutputCompleted)
                )
                if len(outputs) != 1 or any(
                    isinstance(event, ToolCallCompleted) for event in completed.events
                ):
                    raise ValueError("numeric estimation requires one structured result")
                raw_result = json.loads(outputs[0].value)
                if not isinstance(raw_result, dict):
                    raise TypeError("numeric estimation result must be an object")
                # JSON-object mode does not enforce a provider-side schema. Keep
                # harmless extra explanation fields in the recorded response,
                # while validating only the five fields the scheduler consumes.
                raw_result = sanitize_guideline_refs(raw_result,
                    self._guidelines.aliases(operation_id) if self._guidelines is not None else frozenset())
                result = NumericEstimateResponse.model_validate({
                    key: raw_result[key]
                    for key in ("value", "value_lower", "value_upper", "confidence", "rationale", "guideline_refs")
                    if key in raw_result
                })
                if result.value is not None and not task_request.measured_anchors:
                    raise ValueError("point estimate requires original same-parameter measurements")
            except (TypeError, ValueError, ValidationError):
                self._circuit_open = True
                raise
        if (
            task.output_parameter.code_system == "usgs"
            and task.output_parameter.code in {"00400", "00403"}
            and result.value_lower is not None
            and Decimal(result.value_lower) <= 0
            and Decimal(result.value_upper) >= 14
        ):
            result = NumericEstimateResponse(
                value=None,
                value_lower=None,
                value_upper=None,
                confidence=0.0,
                rationale=(
                    "Insufficient information: the model returned only the broad pH domain, "
                    "which is not a useful prediction. " + result.rationale
                )[:4000],
            )
        if self._guidelines is not None:
            await self._guidelines.accept(operation_id,
                GuidelineUsageTarget(kind="numeric_task", task_id=task.task_id, window_id=task.window_id), result.guideline_refs)
        return NumericEstimateResult(
            value=result.value,
            value_lower=result.value_lower,
            value_upper=result.value_upper,
            confidence=result.confidence,
            rationale=result.rationale,
            model_provider=self._model.provider,
            model_name=self._model.model,
            operation_id=str(operation_id),
            prompt_digest=prompt_digest,
        )


def _fragment_projection(fragment) -> dict:
    # Record identities remain in the recorded input references and evidence closure.
    omitted_refs = {
        "input_record_refs", "record_refs", "record_ref", "input_evidence_refs",
        "measured_anchors", "closure_refs",
    }
    payload = fragment.payload.model_dump(
        mode="json", exclude_none=True, exclude=omitted_refs
    )
    return {
        "evidence_id": fragment.evidence_id,
        "parameters": [parameter.model_dump(mode="json") for parameter in fragment.parameter_refs],
        "location": fragment.location.model_dump(mode="json", exclude_none=True),
        "time_range": fragment.time_range.model_dump(mode="json"),
        "collection_status": fragment.collection_status.value,
        "quality_flags": [flag.value for flag in fragment.quality_flags],
        "payload": payload,
    }
