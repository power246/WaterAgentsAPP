import json
from dataclasses import dataclass
from uuid import UUID

from app.application.canonical_json import canonical_json_digest, canonical_json_text
from app.application.guidelines.retrieval import GuidelineSessionKnowledge
from app.application.reasoning.model_models import WaterbodyModelMessage, WaterbodyMessageRole, WaterbodyModelRequest
from app.application.reasoning.model_protocol import CancellationProbe
from app.application.reasoning.batch_planner import ModelFitCapability, ModelSizeEstimator, ReasoningPlannerLimits
from app.domain import ParameterKey
from app.domain.guidelines import GuidelineRetrievalResult, GuidelineUsageTarget, GuidelineVersionV1
from app.ports.clock import ClockPort
from app.ports.errors import WaterbodyModelCancelledError, WaterbodyModelTimeoutError, WaterbodyModelProtocolError


@dataclass(frozen=True)
class GuidelineOperationUsage:
    run_id: UUID
    session_id: UUID
    operation_id: UUID
    phase: str
    target: GuidelineUsageTarget
    aliases: tuple[str, ...]
    versions: tuple[GuidelineVersionV1, ...]


@dataclass(frozen=True)
class GuidelineRequestSelection:
    operation_id: UUID
    phase: str
    aliases: tuple[tuple[str, GuidelineVersionV1], ...]
    status: str
    request_digest: str


class GuidelineModelContext:
    """Session selection and operation-local advisory bindings, independent of scientific facts."""

    def __init__(self, session: GuidelineSessionKnowledge, *, model_fit: ModelFitCapability,
                 size_estimator: ModelSizeEstimator, clock: ClockPort, model_port=None):
        self.session = session
        self._fit = model_fit
        self._estimator = size_estimator
        self._clock = clock
        self._limits = ReasoningPlannerLimits()
        self.operations: dict[UUID, GuidelineRequestSelection] = {}
        self.usages: list[GuidelineOperationUsage] = []
        self._selection: GuidelineRetrievalResult | None = None
        self._evidence: dict[str, object] = {}
        self.transcript = None
        self._model_port = model_port
        self.search_calls = 0
        self.search_tool_calls = 0
        self.search_usage = None
        self.search_request: WaterbodyModelRequest | None = None
        self._search_messages = ()

    async def _search(self, request, cancellation, model_port):
        from app.application.guidelines.search_skill import GuidelineSearchArguments, search_request, search_messages
        from app.application.reasoning.model_protocol import consume_waterbody_model_stream
        from app.application.reasoning.model_models import ToolCallCompleted, StructuredOutputCompleted
        from app.ports.errors import WaterbodyModelPortError

        available = await self.session.availability()
        self._checkpoint(request, cancellation)
        if available is not None:
            return available
        try:
            lookup = search_request(request, self.session.original_question)
        except ValueError:
            return GuidelineRetrievalResult(status="unavailable", safe_error_code="guideline_query_invalid")
        size = self._estimator.estimate(lookup.model,
            serialized_request=canonical_json_text(lookup.model_dump(mode="json")).encode(),
            serialized_response_shape=canonical_json_text({"response_schema": json.loads(lookup.response_schema.schema_text),
                "tool_schema": json.loads(lookup.tool_descriptors[0].input_schema.schema_text),
                "cell_ids": ["guideline-search"]}).encode())
        if (size.input_tokens + size.output_tokens > self._fit.context_window_tokens
                or size.output_tokens > self._fit.max_output_tokens
                or len(canonical_json_text(lookup.model_dump(mode="json")).encode()) > self._limits.max_serialized_input_bytes
                or size.estimated_output_bytes > self._limits.max_serialized_output_bytes):
            return GuidelineRetrievalResult(status="budget_limited")
        self._checkpoint(request, cancellation)
        self.search_request = lookup
        self.search_calls += 1
        try:
            response = await consume_waterbody_model_stream(model_port or self._model_port, lookup, cancellation=cancellation)
            self.search_usage = response.usage
            self._checkpoint(request, cancellation)
            calls = tuple(event for event in response.events if isinstance(event, ToolCallCompleted))
            decisions = tuple(event for event in response.events if isinstance(event, StructuredOutputCompleted))
            if not calls and len(decisions) == 1 and json.loads(decisions[0].value) == {"decision": "skip"}:
                return GuidelineRetrievalResult(status="no_match")
            if len(calls) != 1 or decisions or calls[0].name != "search_guidelines":
                raise ValueError("invalid guideline lookup response")
            args = GuidelineSearchArguments.model_validate_json(calls[0].arguments)
            self.search_tool_calls += 1
            try:
                result = await self.session.search(args.query, deadline=request.deadline)
            except ValueError:
                result = GuidelineRetrievalResult(status="unavailable", safe_error_code="guideline_embedding_configuration_invalid")
            self._search_messages = search_messages(calls[0], result)
            return result
        except (WaterbodyModelCancelledError, WaterbodyModelTimeoutError):
            raise
        except (WaterbodyModelPortError, ValueError):
            return GuidelineRetrievalResult(status="unavailable", safe_error_code="guideline_search_unavailable")

    def bind_transcript(self, store):
        from app.application.guidelines.transcript import GuidelineTranscript

        self.transcript = GuidelineTranscript(self, store)

    def _checkpoint(self, request, cancellation):
        error = (WaterbodyModelCancelledError if cancellation.is_requested() else
                 WaterbodyModelTimeoutError if self._clock.now() >= request.deadline else None)
        if error is not None:
            raise error(provider=request.model.provider, model=request.model.model, operation_id=str(request.operation_id))

    async def prepare(self, request: WaterbodyModelRequest, *, cancellation: CancellationProbe,
                      parameters: tuple[ParameterKey, ...] = (), targets: tuple[str, ...] = (),
                      repair_of: UUID | None = None, versions: set[UUID] | None = None, model_port=None) -> WaterbodyModelRequest:
        self._checkpoint(request, cancellation)
        if request.operation_id in self.operations:
            raise ValueError("guideline model operation must be prepared only once")
        response_shape = canonical_json_text({"response_schema": json.loads(request.response_schema.schema_text),
                                               "cell_ids": targets or (request.batch_id or "operation",)}).encode()

        def estimate(candidate):
            return self._estimator.estimate(request.model,
                serialized_request=canonical_json_text(candidate.model_dump(mode="json")).encode(),
                serialized_response_shape=response_shape)

        base = estimate(request)

        def fits(candidate, size):
            reserve = max(size.output_tokens, base.output_tokens)
            return (size.input_tokens - base.input_tokens <= self.session.policy.knowledge_budget_tokens
                and reserve <= self._fit.max_output_tokens
                and size.input_tokens + reserve <= self._fit.context_window_tokens
                and len(canonical_json_text(candidate.model_dump(mode="json")).encode()) <= self._limits.max_serialized_input_bytes
                and max(size.estimated_output_bytes, base.estimated_output_bytes) <= self._limits.max_serialized_output_bytes)

        if not fits(request, base):
            raise WaterbodyModelProtocolError(provider=request.model.provider, model=request.model.model,
                                              operation_id=str(request.operation_id))
        if repair_of is not None:
            parent = self.operations[repair_of]
            entries, status = parent.aliases, parent.status
        else:
            if self._selection is None and versions is None:
                self._selection = await self._search(request, cancellation, model_port)
            self._checkpoint(request, cancellation)
            if self.transcript is not None:
                await self.transcript.retrieved(self._selection or GuidelineRetrievalResult(status="no_match"))
                self._checkpoint(request, cancellation)
            keys = set(parameters)
            matches = tuple(match for match in (self._selection.matches if self._selection is not None else ())
                if (not keys or keys.intersection((*match.version.content.parameter_keys, *match.version.content.related_parameter_keys)))
                and (versions is None or match.version.version_id in versions))
            entries = tuple((f"G{index+1}_{request.operation_id.hex}", match.version) for index, match in enumerate(matches))
            status = "retrieved" if entries else (
                self._selection.status if self._selection is not None and not self._selection.matches else "no_match")
        selected = request
        history = self._search_messages if not any(message.message_id == "guideline-search:call" for message in request.messages) else ()
        if history:
            candidate = request.model_copy(update={"messages": (*request.messages, *history)})
            if fits(candidate, estimate(candidate)):
                selected = candidate
        while entries:
            block = [{"alias": alias, **version.model_view.model_dump(mode="json")} for alias, version in entries]
            candidate = request.model_copy(update={"messages": (*request.messages, *history, WaterbodyModelMessage(
                message_id=f"advisory:{request.operation_id}", role=WaterbodyMessageRole.USER,
                text=canonical_json_text({"advisory_knowledge": block})))})
            size = estimate(candidate)
            if fits(candidate, size):
                selected = candidate
                break
            if repair_of is not None:
                raise WaterbodyModelProtocolError(provider=request.model.provider, model=request.model.model,
                                                  operation_id=str(request.operation_id))
            entries = entries[:-1]
            status = "retrieved" if entries else "budget_limited"
        self._checkpoint(request, cancellation)
        self.operations[request.operation_id] = GuidelineRequestSelection(request.operation_id, request.phase.value,
            entries, status, canonical_json_digest(selected.model_dump(mode="json")))
        return selected

    def aliases(self, operation_id):
        operation = self.operations.get(operation_id)
        return frozenset(alias for alias, _ in operation.aliases) if operation is not None else frozenset()

    async def accept(self, operation_id, target, aliases):
        operation = self.operations[operation_id]
        available = dict(operation.aliases)
        selected = tuple(alias for alias in dict.fromkeys(aliases or ()) if alias in available)
        if selected:
            usage = GuidelineOperationUsage(self.session.pin.run_id, self.session.session_id,
                operation_id, operation.phase, target, selected, tuple(available[alias] for alias in selected))
            if self.transcript is not None:
                await self.transcript.used(usage)
            self.usages.append(usage)

    def bind_evidence(self, fragments):
        self._evidence = {fragment.evidence_id: fragment for fragment in fragments}

    def synthesis_versions(self, reasoning):
        cells = tuple(cell for batch in reasoning.batches for cell in batch.cells)
        targets = {cell.cell_id for cell in cells}
        used = {version.version_id for usage in self.usages if usage.target.kind == "cell"
                and usage.target.cell_id in targets for version in usage.versions}
        numeric = self._numeric_usages(ref for cell in cells for ref in cell.evidence_refs)
        used.update(version.version_id for usage in numeric for version in usage.versions)
        return used

    def _numeric_usages(self, references):
        pending = list(references)
        seen = set()
        usages = []
        while pending:
            reference = pending.pop()
            if reference in seen:
                continue
            seen.add(reference)
            fragment = self._evidence.get(reference)
            if fragment is None:
                continue
            operation_id = getattr(fragment.payload, "model_operation_id", None)
            for usage in self.usages:
                if (str(usage.operation_id) == operation_id and usage.target.kind == "numeric_task"
                    and usage.target.task_id == getattr(fragment.payload, "task_id", None)):
                    if usage not in usages:
                        usages.append(usage)
            pending.extend(ref.evidence_id for ref in getattr(fragment.payload, "input_evidence_refs", ()))
            converted_input = getattr(fragment.payload, "input_evidence_ref", None)
            if converted_input is not None:
                pending.append(converted_input.evidence_id)
        return tuple(usages)

    async def attach_result_usage(self, result, windows):
        from app.domain.guidelines import GuidelineResultUsageBinding

        if self.transcript is None:
            return result
        bindings = {}
        points = {point.cell_id: point for series in result.series for point in series.points}
        window_ids = {(window.start_time, window.end_time): window.window_id for window in windows}
        by_usage = self.transcript.usage_refs

        def bind(target, usage):
            ref = next(ref for original, ref in by_usage if original is usage)
            bindings.setdefault(target, []).append(ref.item_id)

        for usage in self.usages:
            target = usage.target
            if target.kind == "cell":
                point = points.get(target.cell_id)
                if point is not None and target.window_id == window_ids[(point.start_time, point.end_time)]:
                    bind(target, usage)
            elif target.kind == "window" and target.window_id in set(window_ids.values()):
                bind(target, usage)
            elif target.kind == "overall" or (target.kind == "analysis_step" and target.step_index < len(result.analysis_detail.steps)):
                bind(target, usage)
        for point in points.values():
            for usage in self._numeric_usages(point.evidence_refs):
                bind(GuidelineUsageTarget(kind="cell", cell_id=point.cell_id,
                    window_id=window_ids[(point.start_time, point.end_time)]), usage)
        projected = tuple(GuidelineResultUsageBinding(result_id=result.result_id, target=target,
            usage_item_ids=tuple(dict.fromkeys(item_ids))) for target, item_ids in bindings.items())
        ref = await self.transcript.result_usage(result, projected)
        if ref is None:
            return result
        detail = result.analysis_detail.model_copy(update={"transcript_item_refs": (
            *result.analysis_detail.transcript_item_refs, str(ref.item_id))})
        return result.model_copy(update={"analysis_detail": detail})
