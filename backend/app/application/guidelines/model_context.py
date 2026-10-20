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
                 size_estimator: ModelSizeEstimator, clock: ClockPort):
        self.session = session
        self._fit = model_fit
        self._estimator = size_estimator
        self._clock = clock
        self._limits = ReasoningPlannerLimits()
        self.operations: dict[UUID, GuidelineRequestSelection] = {}
        self.usages: list[GuidelineOperationUsage] = []
        self._selection: GuidelineRetrievalResult | None = None
        self._evidence: dict[str, object] = {}

    def _checkpoint(self, request, cancellation):
        error = (WaterbodyModelCancelledError if cancellation.is_requested() else
                 WaterbodyModelTimeoutError if self._clock.now() >= request.deadline else None)
        if error is not None:
            raise error(provider=request.model.provider, model=request.model.model, operation_id=str(request.operation_id))

    async def prepare(self, request: WaterbodyModelRequest, *, cancellation: CancellationProbe,
                      parameters: tuple[ParameterKey, ...] = (), targets: tuple[str, ...] = (),
                      repair_of: UUID | None = None, versions: set[UUID] | None = None) -> WaterbodyModelRequest:
        self._checkpoint(request, cancellation)
        if request.operation_id in self.operations:
            raise ValueError("guideline model operation must be prepared only once")
        if repair_of is not None:
            parent = self.operations[repair_of]
            entries, status = parent.aliases, parent.status
        else:
            if self._selection is None and versions is None:
                self._selection = await self.session.get_or_load()
            self._checkpoint(request, cancellation)
            keys = set(parameters)
            matches = tuple(match for match in (self._selection.matches if self._selection is not None else ())
                if (not keys or keys.intersection((*match.version.content.parameter_keys, *match.version.content.related_parameter_keys)))
                and (versions is None or match.version.version_id in versions))
            entries = tuple((f"G{index+1}_{request.operation_id.hex}", match.version) for index, match in enumerate(matches))
            status = "retrieved" if entries else (
                self._selection.status if self._selection is not None and not self._selection.matches else "no_match")
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
        selected = request
        while entries:
            block = [{"alias": alias, **version.model_view.model_dump(mode="json")} for alias, version in entries]
            candidate = request.model_copy(update={"messages": (*request.messages, WaterbodyModelMessage(
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

    def accept(self, operation_id, target, aliases):
        operation = self.operations[operation_id]
        available = dict(operation.aliases)
        selected = tuple(alias for alias in dict.fromkeys(aliases or ()) if alias in available)
        if selected:
            self.usages.append(GuidelineOperationUsage(self.session.pin.run_id, self.session.session_id,
                operation_id, operation.phase, target, selected, tuple(available[alias] for alias in selected)))

    def bind_evidence(self, fragments):
        self._evidence = {fragment.evidence_id: fragment for fragment in fragments}

    def synthesis_versions(self, reasoning):
        cells = tuple(cell for batch in reasoning.batches for cell in batch.cells)
        targets = {cell.cell_id for cell in cells}
        used = {version.version_id for usage in self.usages if usage.target.kind == "cell"
                and usage.target.cell_id in targets for version in usage.versions}
        pending = [ref for cell in cells for ref in cell.evidence_refs]
        seen = set()
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
                    used.update(version.version_id for version in usage.versions)
            pending.extend(ref.evidence_id for ref in getattr(fragment.payload, "input_evidence_refs", ()))
            converted_input = getattr(fragment.payload, "input_evidence_ref", None)
            if converted_input is not None:
                pending.append(converted_input.evidence_id)
        return used
