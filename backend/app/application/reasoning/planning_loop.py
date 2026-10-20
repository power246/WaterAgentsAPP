from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Callable, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from app.application.analysis_runs.workflow import WorkflowStopped
from app.application.evidence_tools import (
    EvidenceAcquisitionHandoff,
    EvidenceAcquisitionService,
    EvidenceInspectionPage,
    EvidencePolicyClass,
    EvidenceScopeViolationError,
)
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRule,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
)
from app.application.reasoning.errors import (
    PlanningLoopError,
    ReasoningPlanTooLargeError,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyJsonSchema,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.reasoning.numeric_estimation import ModelNumericEstimator
from app.application.reasoning.prompt_builder import (
    EvidencePlanningPromptInput,
    PromptBudgetRemaining,
    PromptBuilder,
    PromptToolAuthorization,
    WaterbodyStationKind,
    project_authorized_tool_roster,
)
from app.application.reasoning.prompt_policy import (
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.sufficiency import (
    ReasoningCellGate,
    ReasoningGateDecision,
    SealedEvidenceInput,
    seal_evidence_input,
)
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.application.reasoning.topology_coverage import topology_coverage_facts
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    DependencyPromptWindow,
    WorkingContextFact,
    WorkingContextFactKind,
    WorkingContextProjection,
    build_dependency_prompt_block,
    build_working_context_projection,
    require_safe_prompt_text,
)
from app.domain import (
    ContextPayload,
    ConversionPayload,
    DomainModel,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolRequest,
    FacilityRecord,
    ItemReference,
    ModelUsageSummary,
    NonEmptyString,
    NumericDerivedPayload,
    ParameterCapabilityRegistry,
    ParameterKey,
    RelationSupportPayload,
    SourceRecord,
    SourceRecordReference,
    TemporalEstimatePayload,
    UpstreamResultPayload,
)
from app.ports.waterbody_model import WaterbodyModelPort

if TYPE_CHECKING:
    from app.application.reasoning.transcript_store import ReasoningTranscriptStore


_MAX_CONTEXT_FACTS = 120
_MAX_CONTEXT_SUMMARIES = 64
_MAX_CONTEXT_BLOCKERS = 16
_MAX_SUMMARY_RECORDS = 8
_MAX_SUMMARY_BYTES = 1500
_MAX_MODEL_ESTIMATE_SUMMARY_BYTES = 2048
_MAX_RULE_PROPOSALS_PER_TURN = 4


class PlanningConvergenceReason(str, Enum):
    STATION_BACKED_EMPTY = "station_backed_empty"
    EVIDENCE_READY = "evidence_ready"
    NO_LEGAL_ACTIONS = "no_legal_actions"
    NO_PROGRESS = "no_progress"
    MAX_TURNS = "max_turns"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    DEADLINE_REACHED = "deadline_reached"


@dataclass(frozen=True, slots=True)
class PlanningLoopLimits:
    max_turns: int = 6
    max_tool_calls_per_turn: int = 4
    max_numeric_estimation_calls: int = 64

    def __post_init__(self) -> None:
        if (
            type(self.max_turns) is not int
            or type(self.max_tool_calls_per_turn) is not int
            or self.max_turns <= 0
            or self.max_tool_calls_per_turn <= 0
            or type(self.max_numeric_estimation_calls) is not int
            or self.max_numeric_estimation_calls <= 0
            or self.max_numeric_estimation_calls > 64
        ):
            raise ValueError("planning loop limits must be positive integers")


@dataclass(frozen=True, slots=True)
class EvidencePlanningStageCounts:
    preflight_tool_calls: int
    planning_model_calls: int
    agent_tool_calls: int
    rule_proposals: int
    bundle_refs: tuple[ItemReference, ...]
    convergence_reason: PlanningConvergenceReason
    deadline_reached: bool
    final_reasoning_eligible_cells: int
    reasoning_batch_calls: int = 0
    synthesis_calls: int = 0
    numeric_estimation_calls: int = 0
    numeric_failure_codes: tuple[str, ...] = ()
    usage: ModelUsageSummary | None = None


@dataclass(frozen=True, slots=True)
class EvidencePlanningOutcome:
    sealed_input: SealedEvidenceInput
    handoff: EvidenceAcquisitionHandoff
    counts: EvidencePlanningStageCounts


class EvidenceRuleProposal(DomainModel):
    output_parameter: ParameterKey
    input_parameters: Annotated[
        tuple[ParameterKey, ...],
        Field(min_length=1),
    ]
    temporal_relation: EvidenceTemporalRelation
    spatial_relation: EvidenceSpatialRelation
    maximum_inference_strength: EvidenceInferenceStrength
    required_collection_attempt: Literal[False]
    rationale: NonEmptyString

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("input_parameters")
    @classmethod
    def validate_inputs(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        if len(value) != len(set(value)):
            raise ValueError("rule inputs must be unique")
        return value


class _EvidenceReadyDecision(DomainModel):
    decision: Literal["evidence_ready"]


class _EvidenceRulesDecision(DomainModel):
    decision: Literal["evidence_rules_proposed"]
    rules: Annotated[
        tuple[EvidenceRuleProposal, ...],
        Field(min_length=1, max_length=_MAX_RULE_PROPOSALS_PER_TURN),
    ]


_DECISION_ADAPTER = TypeAdapter(
    Annotated[
        _EvidenceReadyDecision | _EvidenceRulesDecision,
        Field(discriminator="decision"),
    ]
)


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class EvidencePlanningLoop:
    """Bounded model orchestration over the single M5 acquisition seam."""

    def __init__(
        self,
        *,
        acquisition: EvidenceAcquisitionService,
        capability_registry: ParameterCapabilityRegistry,
        prompt_builder: PromptBuilder,
        model_port: WaterbodyModelPort,
        limits: PlanningLoopLimits = PlanningLoopLimits(),
        clock: object | None = None,
        operation_id_factory: Callable[[], UUID] | None = None,
        transcript_store: "ReasoningTranscriptStore | None" = None,
    ) -> None:
        self._acquisition = acquisition
        self._capabilities = capability_registry
        self._prompt_builder = prompt_builder
        self._model_port = model_port
        self._limits = limits
        self._clock = clock or _UtcClock()
        self._operation_id_factory = operation_id_factory or uuid4
        self._transcript_store = transcript_store

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline: datetime,
        initial_handoff: EvidenceAcquisitionHandoff | None = None,
    ) -> EvidencePlanningOutcome:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("planning deadline must be timezone-aware")
        scope = task.task.evidence_scope
        handoff = (
            await self._acquisition.start(scope)
            if initial_handoff is None
            else self._acquisition.seal(scope, initial_handoff)
        )
        bundle_refs = [handoff.bundle_ref]
        preflight_calls = len(handoff.tool_result_refs)
        planning_calls = 0
        agent_tool_calls = 0
        rule_proposals = 0
        usage_input_tokens = 0
        usage_output_tokens = 0
        usage_reported = False
        convergence: PlanningConvergenceReason | None = None
        failed_optional_tools: set[str] = set()

        if handoff.stop_before_model:
            convergence = PlanningConvergenceReason.STATION_BACKED_EMPTY

        while convergence is None and planning_calls < self._limits.max_turns:
            if scope.cancel_token.is_requested():
                raise PlanningLoopError("planning_cancelled")
            if self._clock.now() >= deadline:
                convergence = PlanningConvergenceReason.DEADLINE_REACHED
                break
            if _tool_budget_exhausted(handoff):
                convergence = PlanningConvergenceReason.TOOL_BUDGET_EXHAUSTED
                break

            roster = tuple(
                descriptor
                for descriptor in self._acquisition.planning_roster(scope, handoff)
                if descriptor.name not in failed_optional_tools
            )
            if not roster and not _can_append_any_rule(scope, handoff):
                convergence = PlanningConvergenceReason.NO_LEGAL_ACTIONS
                break

            sealed = seal_evidence_input(task, handoff, self._capabilities)
            _require_batch_representable_gates(sealed)
            context = await _working_context(task, handoff, sealed)
            request = self._prompt_builder.build_evidence_planning(
                EvidencePlanningPromptInput(
                    operation_id=self._operation_id_factory(),
                    model=task.task.waterbody_model,
                    deadline=deadline,
                    context=context,
                    response_schema=_planning_response_schema(),
                    station_kind=(
                        WaterbodyStationKind.STATION_BACKED
                        if scope.station_refs
                        else WaterbodyStationKind.STATIONLESS
                    ),
                    gates=_project_planning_gates(
                        sealed.gates, context.evidence_summaries,
                    ),
                    budget=_prompt_budget(handoff, self._limits, planning_calls),
                    authorized_tools=_unit_scoped_tool_roster(
                        project_authorized_tool_roster(roster),
                        task, self._capabilities,
                    ),
                    unit_preferences=task.context.unit_preferences,
                    authorized_parameters=tuple(
                        dict.fromkeys(
                            (
                                *scope.requested_parameters,
                                *scope.supporting_parameters,
                                *scope.intermediate_parameters,
                                *(
                                    parameter
                                    for rule in handoff.rule_plan.effective_rules
                                    for parameter in (
                                        rule.output_parameter,
                                        *rule.input_parameters,
                                    )
                                ),
                            )
                        )
                    ),
                )
            )
            response = await consume_waterbody_model_stream(
                self._model_port,
                request,
                cancellation=scope.cancel_token,
            )
            planning_calls += 1
            if response.usage is not None:
                usage_reported = True
                usage_input_tokens += response.usage.input_tokens or 0
                usage_output_tokens += response.usage.output_tokens or 0
            tool_events = tuple(
                event for event in response.events if isinstance(event, ToolCallCompleted)
            )
            structured = next(
                (
                    event
                    for event in response.events
                    if isinstance(event, StructuredOutputCompleted)
                ),
                None,
            )
            visible = "".join(
                event.text
                for event in response.events
                if isinstance(event, VisibleTextDelta)
            )
            decision = _parse_decision(structured)
            if len(tool_events) > self._limits.max_tool_calls_per_turn:
                raise PlanningLoopError("planning_tool_call_limit_exceeded")
            if isinstance(decision, _EvidenceReadyDecision) and tool_events:
                raise PlanningLoopError("invalid_planning_response")
            if decision is None and not tool_events:
                # Visible prose is useful only as an explanation accompanying an action.
                _ = visible
                raise PlanningLoopError("invalid_planning_response")
            decision_bundle_ref = handoff.bundle_ref

            if tool_events:
                offered_optional_tools = {
                    descriptor.name
                    for descriptor in roster
                    if descriptor.policy_class is EvidencePolicyClass.OPTIONAL
                }
                failed_before_turn = frozenset(failed_optional_tools)
                for event in tool_events:
                    if self._clock.now() >= deadline:
                        convergence = PlanningConvergenceReason.DEADLINE_REACHED
                        break
                    tool_request = await _tool_request(
                        scope, handoff, event,
                        preferences=task.context.unit_preferences,
                        registry=self._capabilities,
                    )
                    if tool_request.tool_name in failed_before_turn:
                        raise PlanningLoopError("invalid_planning_response")
                    previous_results = len(handoff.tool_result_refs)
                    executions = []
                    handoff = await self._acquisition.execute_optional(
                        scope,
                        (tool_request,),
                        handoff,
                        max_calls=self._limits.max_tool_calls_per_turn,
                        on_execution=executions.append,
                    )
                    if (
                        tool_request.tool_name in offered_optional_tools
                        and executions
                        and executions[0].result.execution_status
                        is EvidenceToolExecutionStatus.FAILED
                        and executions[0].result.safe_error is not None
                        and (
                            executions[0].result.safe_error.code.startswith(
                                "evidence_provider_"
                            )
                            or executions[0].result.safe_error.code
                            == "evidence_collection_incomplete"
                        )
                    ):
                        failed_optional_tools.add(tool_request.tool_name)
                    agent_tool_calls += (
                        len(handoff.tool_result_refs) - previous_results
                    )
                    bundle_refs.append(handoff.bundle_ref)
                    if scope.cancel_token.is_requested():
                        raise PlanningLoopError("planning_cancelled")

            deadline_reached = (
                convergence is PlanningConvergenceReason.DEADLINE_REACHED
            )
            if not deadline_reached and isinstance(decision, _EvidenceRulesDecision):
                try:
                    rules = tuple(
                        _materialize_rule(scope.session_id, proposal)
                        for proposal in decision.rules
                    )
                except ValueError as error:
                    raise PlanningLoopError("invalid_planning_response") from error
                known_signatures = {
                    (
                        rule.output_parameter,
                        rule.input_parameters,
                        rule.temporal_relation,
                        rule.spatial_relation,
                        rule.maximum_inference_strength,
                    )
                    for rule in handoff.rule_plan.effective_rules
                }
                new_rules = []
                for rule in rules:
                    signature = (
                        rule.output_parameter,
                        rule.input_parameters,
                        rule.temporal_relation,
                        rule.spatial_relation,
                        rule.maximum_inference_strength,
                    )
                    if signature in known_signatures:
                        continue
                    known_signatures.add(signature)
                    new_rules.append(rule)
                if new_rules:
                    previous_count = len(handoff.rule_plan.appended_rules)
                    try:
                        handoff = await self._acquisition.append_rules(
                            scope,
                            tuple(new_rules),
                            handoff,
                        )
                    except EvidenceScopeViolationError as error:
                        raise PlanningLoopError("invalid_planning_response") from error
                    rule_proposals += (
                        len(handoff.rule_plan.appended_rules) - previous_count
                    )
                    bundle_refs.append(handoff.bundle_ref)
                elif not tool_events:
                    convergence = PlanningConvergenceReason.NO_PROGRESS
            elif not deadline_reached and isinstance(decision, _EvidenceReadyDecision):
                convergence = PlanningConvergenceReason.EVIDENCE_READY

            if self._transcript_store is not None:
                from app.application.reasoning.transcript_models import (
                    ReasoningDecisionType,
                )

                if tool_events:
                    await self._transcript_store.write_planning_decision(
                        request,
                        decision_type=ReasoningDecisionType.TOOL_CALLS_REQUESTED,
                        bundle_ref=decision_bundle_ref,
                        rationale=(
                            visible if visible.strip()
                            else "The model requested authorized evidence tools."
                        ),
                        tool_call_ids=tuple(
                            event.provider_call_id for event in tool_events
                        ),
                    )
                if not deadline_reached and isinstance(
                    decision,
                    _EvidenceRulesDecision,
                ):
                    await self._transcript_store.write_planning_decision(
                        request,
                        decision_type=ReasoningDecisionType.EVIDENCE_RULES_PROPOSED,
                        bundle_ref=decision_bundle_ref,
                        rationale="The model proposed bounded evidence rules.",
                    )
                elif not deadline_reached and isinstance(
                    decision,
                    _EvidenceReadyDecision,
                ):
                    await self._transcript_store.write_planning_decision(
                        request,
                        decision_type=ReasoningDecisionType.EVIDENCE_READY,
                        bundle_ref=decision_bundle_ref,
                        rationale=(
                            visible if visible.strip()
                            else "The model reported that evidence planning is complete."
                        ),
                    )
            if deadline_reached:
                continue

        if convergence is None:
            convergence = PlanningConvergenceReason.MAX_TURNS

        numeric_calls = 0
        if (
            handoff.numeric_estimation_allowed
            and convergence is not PlanningConvergenceReason.DEADLINE_REACHED
        ):
            estimator = ModelNumericEstimator(
                model_port=self._model_port,
                model=task.task.waterbody_model,
                bundle_ref=handoff.bundle_ref,
                capability_registry=self._capabilities,
                deadline=deadline,
                clock=self._clock,
                max_calls=self._limits.max_numeric_estimation_calls,
                operation_id_factory=self._operation_id_factory,
            )
            try:
                handoff = await self._acquisition.resolve_numeric(
                    scope, handoff, estimator=estimator
                )
            except WorkflowStopped:
                raise PlanningLoopError("planning_cancelled") from None
            numeric_calls = estimator.calls
            usage_input_tokens += estimator.input_tokens
            usage_output_tokens += estimator.output_tokens
            usage_reported |= estimator.usage_reported
            if bundle_refs[-1] != handoff.bundle_ref:
                bundle_refs.append(handoff.bundle_ref)
            if (self._clock.now() >= deadline
                    or "numeric_estimation_deadline_exceeded" in handoff.block_facts):
                convergence = PlanningConvergenceReason.DEADLINE_REACHED

        handoff = self._acquisition.seal(scope, handoff)
        sealed = seal_evidence_input(task, handoff, self._capabilities)
        _require_batch_representable_gates(sealed)
        return EvidencePlanningOutcome(
            sealed_input=sealed,
            handoff=handoff,
            counts=EvidencePlanningStageCounts(
                preflight_tool_calls=preflight_calls,
                planning_model_calls=planning_calls,
                numeric_estimation_calls=numeric_calls,
                numeric_failure_codes=tuple(
                    fact for fact in handoff.block_facts if fact.startswith("numeric_")
                ),
                agent_tool_calls=agent_tool_calls,
                rule_proposals=rule_proposals,
                bundle_refs=tuple(bundle_refs),
                convergence_reason=convergence,
                deadline_reached=(
                    convergence is PlanningConvergenceReason.DEADLINE_REACHED
                ),
                final_reasoning_eligible_cells=sum(
                    gate.decision is ReasoningGateDecision.SUPPORTED
                    for gate in sealed.gates
                ),
                usage=(
                    ModelUsageSummary(
                        input_tokens=usage_input_tokens,
                        output_tokens=usage_output_tokens,
                    )
                    if usage_reported
                    else None
                ),
            ),
        )


async def build_reasoning_working_context(
    task: ValidatedWaterbodyAgentTask,
    outcome: EvidencePlanningOutcome,
) -> WorkingContextProjection:
    """Build the one sealed context reused by batch planning in the runner."""

    return await _working_context(
        task,
        outcome.handoff,
        outcome.sealed_input,
    )


def build_reasoning_evidence_summaries(
    handoff: EvidenceAcquisitionHandoff,
) -> tuple[BoundedEvidenceSummary, ...]:
    return _evidence_summaries(
        handoff,
        tuple(sorted(
            handoff.fragment_allowlist,
            key=lambda item: (item.evidence_id, item.item_id),
        )),
        require_upstream_projection=True,
    )


def _parse_decision(
    event: StructuredOutputCompleted | None,
) -> _EvidenceReadyDecision | _EvidenceRulesDecision | None:
    if event is None:
        return None
    try:
        return _DECISION_ADAPTER.validate_json(event.value)
    except Exception as error:
        raise PlanningLoopError("invalid_planning_response") from error


def _materialize_rule(
    session_id: str,
    proposal: EvidenceRuleProposal,
) -> EvidenceRule:
    return create_evidence_rule(
        output_parameter=proposal.output_parameter,
        input_parameters=proposal.input_parameters,
        temporal_relation=proposal.temporal_relation,
        spatial_relation=proposal.spatial_relation,
        maximum_inference_strength=proposal.maximum_inference_strength,
        required_collection_attempt=False,
        rationale=proposal.rationale,
        origin=EvidenceRuleOrigin.WATERBODY_AGENT,
        origin_session_id=session_id,
    )


def _unit_scoped_tool_roster(
    roster: tuple[PromptToolAuthorization, ...],
    task: ValidatedWaterbodyAgentTask,
    registry: ParameterCapabilityRegistry,
) -> tuple[PromptToolAuthorization, ...]:
    allowed = {item.target_unit_id for item in task.context.unit_preferences}
    for key in (
        *task.task.evidence_scope.requested_parameters,
        *task.task.evidence_scope.supporting_parameters,
    ):
        capability = registry.get(key)
        if capability is not None:
            allowed.add(capability.unit_policy_id)
    return tuple(
        tool.model_copy(update={"allowed_target_unit_ids": tuple(sorted(allowed))})
        if tool.name == "convert_evidence_unit" else tool
        for tool in roster
    )


async def _tool_request(
    scope,
    handoff: EvidenceAcquisitionHandoff,
    event: ToolCallCompleted,
    *,
    preferences=(),
    registry=None,
) -> EvidenceToolRequest:
    try:
        payload = json.loads(event.arguments)
        if set(payload) != {"window_ids", "parameter_keys", "options"}:
            raise ValueError
        options = dict(payload["options"])
        if event.name == "convert_evidence_unit" and registry is not None:
            keys = tuple(ParameterKey.model_validate(value) for value in payload["parameter_keys"])
            if len(keys) != 1:
                raise ValueError
            capability = registry.get(keys[0])
            if capability is None:
                raise ValueError
            preference = next((item for item in preferences if item.parameter == keys[0]), None)
            allowed = {capability.unit_policy_id}
            if preference is not None:
                allowed.add(preference.target_unit_id)
            if options.get("target_unit_id") not in allowed:
                raise ValueError
        if "fragment_refs" in options:
            by_id = {
                reference.evidence_id: reference
                for reference in handoff.fragment_allowlist
            }
            options["fragment_refs"] = tuple(
                by_id[evidence_id] for evidence_id in options["fragment_refs"]
            )
        if "source_record_refs" in options:
            references: list[SourceRecordReference] = []
            for item_id in options["source_record_refs"]:
                records = await handoff.read_port.read_source_records(scope, item_id)
                references.extend(
                    SourceRecordReference(item_id=item_id, record_id=record.record_id)
                    for record in records
                )
            options["source_record_refs"] = tuple(references)
        return EvidenceToolRequest(
            tool_call_id=event.provider_call_id,
            session_id=scope.session_id,
            tool_name=event.name,
            purpose=f"Agent-selected {event.name} evidence action.",
            window_ids=tuple(payload["window_ids"]),
            parameter_keys=tuple(
                ParameterKey.model_validate(value)
                for value in payload["parameter_keys"]
            ),
            options=EvidenceToolOptions.model_validate(options),
        )
    except Exception as error:
        raise PlanningLoopError("invalid_planning_tool_arguments") from error


def _prompt_field_text(value: str) -> tuple[str, bool]:
    selected = value[:80]
    try:
        require_safe_prompt_text(selected)
    except ValueError:
        return "[REDACTED]", True
    return selected, len(selected) != len(value)


def _source_record_view(record: SourceRecord) -> tuple[dict[str, object], bool]:
    allowed = {
        "record_type", "observed_time", "valid_time_range", "value",
        "raw_unit", "parameter", "weather_variable", "window_id",
        "result_kind", "result_source", "confidence", "upstream_session_id",
    }
    view = record.model_dump(mode="json", include=allowed, exclude_none=True)
    location_fields = {"comid", "station_id"}
    if isinstance(record, FacilityRecord):
        location_fields.update(
            {"point", "provider_location_id", "distance_to_target_km"}
        )
    view["location"] = record.location.model_dump(
        mode="json", include=location_fields, exclude_none=True
    )
    truncated = False
    if isinstance(record, FacilityRecord):
        view["record_id"], shortened = _prompt_field_text(record.record_id)
        truncated |= shortened
        view["spatially_in_scope"] = record.spatially_in_scope
        provider_location_id = view["location"].get("provider_location_id")
        if provider_location_id is not None:
            view["location"]["provider_location_id"], shortened = (
                _prompt_field_text(provider_location_id)
            )
            truncated |= shortened
        for field in ("registry_id", "facility_id", "permit_id", "program", "status"):
            value = getattr(record, field)
            if value is not None:
                view[field], shortened = _prompt_field_text(value)
                truncated |= shortened
        attributes = {}
        allowed_attributes = {
            "facility_name", "permit_name", "permit_type", "facility_type",
            "city", "state", "receiving_waterbody",
        }
        for name, value in record.public_attributes:
            if name in allowed_attributes:
                attributes[name], shortened = _prompt_field_text(value)
                truncated |= shortened
        if attributes:
            view["public_attributes"] = attributes
    return view, truncated


def _bounded_fragment_summary(
    fragment: EvidenceFragment,
    source_records: tuple[SourceRecord, ...],
    inspection_pages: tuple[EvidenceInspectionPage, ...],
) -> tuple[str, bool]:
    payload = fragment.payload
    if isinstance(payload, NumericDerivedPayload):
        # Explicit calculations retain all input bindings; model estimates may
        # summarize a larger evidence set within the working-context limit.
        projection = {
            "type": fragment.data_type,
            "param": (
                f"{fragment.parameter_refs[0].code_system}:"
                f"{fragment.parameter_refs[0].code}"
            ),
            "range": (
                f"{fragment.time_range.start.isoformat().replace('+00:00', 'Z')}/"
                f"{fragment.time_range.end.isoformat().replace('+00:00', 'Z')}"
            ),
            "flags": ",".join(flag.value for flag in fragment.quality_flags),
            "value": payload.value,
            "unit": payload.unit,
            "method_id": payload.method_id,
            "version": payload.method_version,
            "task_id": payload.task_id,
            "plan_digest": payload.plan_digest,
            "plan_ref": payload.plan_item_ref.item_id,
            "confidence_cap": payload.confidence_cap,
            "inputs": tuple(
                (reference.evidence_id, window_id)
                for reference, window_id in zip(
                    payload.input_evidence_refs,
                    payload.input_window_ids,
                    strict=True,
                )
            ),
        }
        shortened = False
        if payload.method_kind == "model_estimate":
            anchors = sorted(
                payload.measured_anchors,
                key=lambda anchor: (
                    anchor.distance_km is None,
                    anchor.distance_km if anchor.distance_km is not None else 0,
                    anchor.origin_comid,
                    anchor.station_id or "",
                ),
            )
            anchor_views = [{
                "origin_comid": anchor.origin_comid,
                "station_id": anchor.station_id,
                "distance_km": anchor.distance_km,
                **(
                    {"distance_basis": anchor.distance_basis}
                    if anchor.distance_basis is not None else {}
                ),
                "path_waterbody_count": len(anchor.path_comids),
            } for anchor in anchors[:4]]
            input_bindings = list(projection["inputs"][:8])
            projection.update({
                "value_lower": payload.value_lower,
                "value_upper": payload.value_upper,
                "method_kind": payload.method_kind,
                "inference_basis": payload.inference_basis,
                "rationale": (payload.rationale or "")[:240],
                "measured_anchors": anchor_views,
                "input_count": len(payload.input_evidence_refs),
                "anchor_count": len(payload.measured_anchors),
                "inputs": input_bindings,
            })
            shortened = (
                bool(anchors)
                or len(payload.rationale or "") > 240
                or len(payload.input_evidence_refs) > 8
            )
            while (
                len(canonical_json_text(projection).encode("utf-8"))
                > _MAX_MODEL_ESTIMATE_SUMMARY_BYTES
                and len(anchor_views) > 1
            ):
                anchor_views.pop()
                shortened = True
            while (
                len(canonical_json_text(projection).encode("utf-8"))
                > _MAX_MODEL_ESTIMATE_SUMMARY_BYTES
                and input_bindings
            ):
                input_bindings.pop()
                shortened = True
            if (
                len(canonical_json_text(projection).encode("utf-8"))
                > _MAX_MODEL_ESTIMATE_SUMMARY_BYTES
            ):
                rationale = projection["rationale"]
                low, high = 0, len(rationale)
                while low < high:
                    middle = (low + high + 1) // 2
                    projection["rationale"] = rationale[:middle]
                    if (
                        len(canonical_json_text(projection).encode("utf-8"))
                        <= _MAX_MODEL_ESTIMATE_SUMMARY_BYTES
                    ):
                        low = middle
                    else:
                        high = middle - 1
                projection["rationale"] = rationale[:low]
                shortened |= low < len(rationale)
        summary = canonical_json_text(projection)
        limit = (
            _MAX_MODEL_ESTIMATE_SUMMARY_BYTES
            if payload.method_kind == "model_estimate" else _MAX_SUMMARY_BYTES
        )
        if len(summary.encode("utf-8")) > limit:
            raise PlanningLoopError("numeric_evidence_prompt_projection_too_large")
        return summary, shortened
    payload_fields = {
        "payload_type", "displayed_value", "displayed_unit", "value", "unit",
        "method", "valid_input_count", "suspicious_input_count",
        "invalid_input_count", "upstream_session_id", "result_ref",
        "inference_strength", "status",
    }
    payload_truncated = False
    upstream_anchor_views: list[dict[str, object]] | None = None
    payload_view = payload.model_dump(
        mode="json", include=payload_fields, exclude_none=True
    )
    if payload.payload_type == "historical_inference":
        payload_view["cell_projection"] = payload.cell_projection.model_dump(
            mode="json",
            include={
                "parameter", "window_id", "time_range", "outcome_status",
                "representative_value", "unit_id", "trend", "risk_level",
                "confidence", "source", "quality_flags",
            },
            exclude_none=True,
        )
    if payload.payload_type == "upstream_result" and payload.cell_projection is not None:
        cell = payload.cell_projection
        cell_view = cell.model_dump(
            mode="json", exclude={"measured_anchors"}, exclude_none=True
        )
        anchors = sorted(
            cell.measured_anchors,
            key=lambda anchor: (
                anchor.distance_km is None,
                anchor.distance_km if anchor.distance_km is not None else 0,
                anchor.origin_comid,
                anchor.station_id or "",
            ),
        )
        upstream_anchor_views = [
            {
                "origin_comid": anchor.origin_comid,
                "station_id": anchor.station_id,
                "distance_km": anchor.distance_km,
                **(
                    {"distance_basis": anchor.distance_basis}
                    if anchor.distance_basis is not None else {}
                ),
                "evidence_time_range": anchor.evidence_time_range.model_dump(mode="json"),
                "path_waterbody_count": len(anchor.path_comids),
                "source_record_count": len(anchor.source_record_refs),
            }
            for anchor in anchors[:4]
        ]
        cell_view["measured_anchor_count"] = len(anchors)
        cell_view["measured_anchors"] = upstream_anchor_views
        payload_view["cell_projection"] = cell_view
        payload_truncated = bool(anchors)
    if isinstance(payload, RelationSupportPayload):
        payload_view["input_evidence_refs"] = tuple(
            reference.evidence_id for reference in payload.input_evidence_refs
        )
        payload_view["rationale"] = payload.rationale
    if isinstance(payload, TemporalEstimatePayload):
        payload_view["input_evidence_refs"] = tuple(
            reference.evidence_id for reference in payload.input_evidence_refs
        )
        payload_view["anchor_window_ids"] = payload.anchor_window_ids
        payload_view["anchor_distance_seconds"] = payload.anchor_distance_seconds
        if payload.plan_item_ref is not None:
            payload_view["plan_item_ref"] = payload.plan_item_ref.item_id
            payload_view["task_id"] = payload.task_id
            payload_view["plan_digest"] = payload.plan_digest
            payload_view["method_registry_digest"] = payload.method_registry_digest
    if isinstance(payload, ConversionPayload):
        payload_view["input_evidence_ref"] = payload.input_evidence_ref.evidence_id
    if isinstance(payload, ContextPayload):
        selected_fields = {}
        allowed_fields = {
            "program", "status", "facility_id", "weather_variable", "value",
            "unit", "statistic", "result_kind", "record_type",
        }
        for name, value in payload.summary_fields:
            if name in allowed_fields:
                selected_fields[name], shortened = _prompt_field_text(value)
                payload_truncated |= shortened
        payload_view["summary_fields"] = selected_fields
    base = {
        "data_type": fragment.data_type,
        "collection_status": fragment.collection_status,
        "applicability": fragment.applicability,
        "quality_class": fragment.quality_class,
        "quality_flags": fragment.quality_flags,
        "coverage_status": fragment.coverage.status,
        "result_eligibility": fragment.result_eligibility,
        "parameters": fragment.parameter_refs,
        "time_range": fragment.time_range,
        "relation_id": fragment.relation_id,
        "payload": payload_view,
    }
    if isinstance(payload, RelationSupportPayload):
        preview = payload.rationale[:320]
        payload_view["rationale"] = preview
        if len(canonical_json_text({**base, "records": []}).encode("utf-8")) > _MAX_SUMMARY_BYTES:
            low, high = 0, len(preview)
            while low < high:
                middle = (low + high + 1) // 2
                payload_view["rationale"] = preview[:middle]
                if (
                    len(canonical_json_text({**base, "records": []}).encode("utf-8"))
                    <= _MAX_SUMMARY_BYTES
                ):
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise PlanningLoopError("evidence_prompt_projection_too_large")
            preview = preview[:low]
            payload_view["rationale"] = preview
        payload_truncated |= len(preview) < len(payload.rationale)
    latest_pages = {
        page.source_item_id: page for page in inspection_pages
    }
    if latest_pages:
        base["inspections"] = tuple(
            {
                "source_item_id": item_id,
                "offset": page.offset,
                "total_records": page.total_records,
                "next_offset": page.next_offset,
            }
            for item_id, page in sorted(latest_pages.items())
        )
    records = []
    seen_record_ids = set()
    for record in (
        *(record for _, page in sorted(latest_pages.items()) for record in page.records),
        *source_records,
    ):
        if record.record_id not in seen_record_ids:
            seen_record_ids.add(record.record_id)
            records.append(record)
    base["records"] = []
    truncated = payload_truncated or len(records) > _MAX_SUMMARY_RECORDS or any(
        page.next_offset is not None for page in latest_pages.values()
    )
    for record in records[:_MAX_SUMMARY_RECORDS]:
        view, field_truncated = _source_record_view(record)
        truncated |= field_truncated
        candidate = {**base, "records": [*base["records"], view]}
        if (
            isinstance(record, FacilityRecord)
            and len(canonical_json_text(candidate).encode("utf-8")) > _MAX_SUMMARY_BYTES
        ):
            # Keep the identity, status, location, name and type when a provider
            # supplies many optional facility labels.
            attributes = view.get("public_attributes", {})
            view["public_attributes"] = {
                key: attributes[key]
                for key in ("facility_name", "facility_type")
                if key in attributes
            }
            view.pop("registry_id", None)
            view["location"].pop("provider_location_id", None)
            candidate = {**base, "records": [*base["records"], view]}
            truncated = True
        if len(canonical_json_text(candidate).encode("utf-8")) > _MAX_SUMMARY_BYTES:
            truncated = True
            break
        base["records"].append(view)
    if upstream_anchor_views is not None:
        while (
            upstream_anchor_views
            and len(canonical_json_text(base).encode("utf-8")) > _MAX_SUMMARY_BYTES
        ):
            upstream_anchor_views.pop()
            truncated = True
    summary = canonical_json_text(base)
    if len(summary.encode("utf-8")) > _MAX_SUMMARY_BYTES:
        raise PlanningLoopError("evidence_prompt_projection_too_large")
    try:
        require_safe_prompt_text(summary)
    except ValueError as error:
        raise PlanningLoopError("evidence_prompt_projection_invalid") from error
    return summary, truncated


def _require_batch_representable_gates(sealed: SealedEvidenceInput) -> None:
    """Reject a cell that cannot fit any exact-reference reasoning batch."""
    if any(
        gate.decision is ReasoningGateDecision.SUPPORTED
        and len(gate.allowed_evidence_refs) > _MAX_CONTEXT_SUMMARIES
        for gate in sealed.gates
    ):
        raise ReasoningPlanTooLargeError()


async def _working_context(
    task: ValidatedWaterbodyAgentTask,
    handoff: EvidenceAcquisitionHandoff,
    sealed: SealedEvidenceInput,
):
    scope = task.task.evidence_scope
    facts: list[WorkingContextFact] = []
    for index, code in enumerate(
        tuple(dict.fromkeys(handoff.block_facts))[:_MAX_CONTEXT_BLOCKERS]
    ):
        facts.append(
            WorkingContextFact(
                fact_id=f"tool_error:{index:03d}",
                kind=WorkingContextFactKind.BLOCKER,
                summary=f"Evidence operation failed safely with code {code}.",
            )
        )
    facts.extend(topology_coverage_facts(scope)[: _MAX_CONTEXT_FACTS - len(facts)])
    remaining_fact_slots = _MAX_CONTEXT_FACTS - len(facts)
    for gate in sealed.gates[:remaining_fact_slots]:
        kind = (
            WorkingContextFactKind.BLOCKER
            if gate.decision is ReasoningGateDecision.BLOCKED
            else WorkingContextFactKind.MISSING
            if gate.decision is ReasoningGateDecision.UNSUPPORTED
            else WorkingContextFactKind.GATE
        )
        facts.append(
            WorkingContextFact(
                fact_id=f"gate:{gate.cell_id}",
                kind=kind,
                summary=(
                    f"Cell {gate.cell_id} is {gate.decision.value}; "
                    f"reasons={','.join(gate.reasons) or 'none'}."
                ),
            )
        )

    gate_evidence_ids = {
        evidence_id
        for gate in sealed.gates
        for evidence_id in gate.allowed_evidence_refs
    }
    summaries = _evidence_summaries(
        handoff,
        tuple(sorted(
            handoff.fragment_allowlist,
            key=lambda item: (
                item.evidence_id not in gate_evidence_ids,
                item.evidence_id,
                item.item_id,
            ),
        ))[:_MAX_CONTEXT_SUMMARIES],
    )
    scope_digest = canonical_json_digest(
        {
            "run_id": task.task.run_id,
            "session_id": task.task.session_id,
            "target_comid": task.task.target_comid,
            "context_ref": task.task.analysis_context_ref,
            "topology_ref": task.task.topology_ref,
            "parameters": scope.requested_parameters,
            "supporting_parameters": scope.supporting_parameters,
            "windows": tuple(window.window_id for window in scope.time_windows),
        }
    )
    dependency_context = (
        build_dependency_prompt_block(
            task.task.dependency_manifest,
            mode=task.context.mode,
            windows=tuple(
                DependencyPromptWindow(
                    window_id=window.window_id,
                    mode=task.context.time_segments[window.segment_index].mode,
                )
                for window in scope.time_windows
            ),
        )
        if task.task.dependency_manifest is not None
        else None
    )
    required_summary_refs = {
        reference
        for gate in sealed.gates
        for reference in gate.allowed_evidence_refs
    }
    minimum_summaries = sum(
        summary.evidence_ref in required_summary_refs for summary in summaries
    )
    for count in range(len(summaries), minimum_summaries - 1, -1):
        selected = summaries[:count]
        prompt_gates = _project_planning_gates(sealed.gates, selected)
        projected_facts = list(facts)
        selected_refs = {summary.evidence_ref for summary in selected}
        for gate, projected in zip(sealed.gates, prompt_gates, strict=True):
            omitted_contextual = (
                len(gate.contextual_refs) - len(projected.contextual_refs)
            )
            omitted_allowed = sum(
                reference not in selected_refs
                for reference in gate.allowed_evidence_refs
            )
            if (
                (omitted_contextual or omitted_allowed)
                and len(projected_facts) < _MAX_CONTEXT_FACTS
            ):
                projected_facts.append(
                    WorkingContextFact(
                        fact_id=f"coverage:{gate.cell_id}",
                        kind=WorkingContextFactKind.COVERAGE,
                        summary=(
                            f"Cell {gate.cell_id}: {omitted_allowed} allowed evidence "
                            "summaries and "
                            f"{omitted_contextual} contextual evidence references "
                            "omitted from this prompt."
                        ),
                    )
                )
        allowlist = tuple(sorted({
            *(
                reference
                for gate in prompt_gates
                for reference in (
                    *gate.allowed_evidence_refs,
                    *gate.contextual_refs,
                    *gate.collection_fact_refs,
                    *gate.blocker_refs,
                )
            ),
            *(summary.evidence_ref for summary in selected),
            *(
                reference
                for summary in selected
                for reference in (
                    *summary.source_record_item_refs,
                    *summary.dependency_evidence_refs,
                )
            ),
        }))
        try:
            return build_working_context_projection(
                run_id=task.task.run_id,
                session_id=task.task.session_id,
                target_comid=task.task.target_comid,
                scope_digest=scope_digest,
                bundle_ref=handoff.bundle_ref,
                bundle_digest=handoff.bundle.bundle_id,
                rule_plan_digest=handoff.rule_plan_digest,
                capability_registry_digest=handoff.capability_registry_digest,
                reference_allowlist=allowlist,
                recent_messages=(),
                facts=tuple(projected_facts),
                evidence_summaries=selected,
                dependency_context=dependency_context,
            )
        except ValidationError as error:
            if not _context_size_error(error):
                raise
    raise PlanningLoopError("evidence_prompt_projection_too_large")


def _project_planning_gates(
    gates: tuple[ReasoningCellGate, ...],
    summaries: tuple[BoundedEvidenceSummary, ...],
) -> tuple[ReasoningCellGate, ...]:
    visible = {summary.evidence_ref for summary in summaries}
    return tuple(
        gate.model_copy(update={
            "contextual_refs": tuple(
                reference
                for reference in gate.contextual_refs
                if reference in visible
            ),
        })
        for gate in gates
    )


def _context_size_error(error: ValidationError) -> bool:
    issues = error.errors()
    return bool(issues) and all(
        "working context exceeds the bounded byte limit" in item["msg"]
        for item in issues
    )


def _evidence_summaries(
    handoff: EvidenceAcquisitionHandoff,
    references: tuple[EvidenceFragmentReference, ...],
    *,
    require_upstream_projection: bool = False,
) -> tuple[BoundedEvidenceSummary, ...]:
    summaries = []
    inspected_source_items: set[str] = set()
    fragments_by_ref = dict(handoff.validated_inputs.fragments)
    source_cache = {
        item_id: {record.record_id: record for record in records}
        for item_id, records in handoff.validated_inputs.source_records
    }
    for reference in references:
        fragment = fragments_by_ref[reference]
        if (
            require_upstream_projection
            and isinstance(fragment.payload, UpstreamResultPayload)
            and fragment.payload.cell_projection is None
        ):
            raise PlanningLoopError("evidence_prompt_projection_invalid")
        source_item_ids = tuple(dict.fromkeys(
            item.item_id for item in fragment.source_record_refs
        ))
        source_records = tuple(
            source_cache[item.item_id][item.record_id]
            for item in fragment.source_record_refs
        )
        inspection_pages = handoff.executor.inspection_pages(
            handoff.run_id,
            tuple(
                item_id for item_id in source_item_ids
                if item_id not in inspected_source_items
            ),
        )
        inspected_source_items.update(
            page.source_item_id for page in inspection_pages
        )
        summary, truncated = _bounded_fragment_summary(
            fragment, source_records, inspection_pages
        )
        summaries.append(
            BoundedEvidenceSummary(
                evidence_ref=reference.evidence_id,
                summary=summary,
                source_record_item_refs=tuple(
                    dict.fromkeys(
                        item.item_id for item in fragment.source_record_refs
                    )
                ),
                dependency_evidence_refs=(
                    tuple(
                        item.evidence_id
                        for item in fragment.payload.input_evidence_refs
                    )
                    if isinstance(
                        fragment.payload,
                        (
                            RelationSupportPayload,
                            TemporalEstimatePayload,
                            NumericDerivedPayload,
                        ),
                    )
                    else (fragment.payload.input_evidence_ref.evidence_id,)
                    if isinstance(fragment.payload, ConversionPayload)
                    else ()
                ),
                series_refs=(
                    (fragment.series_identity,)
                    if fragment.series_identity is not None
                    else ()
                ),
                truncated=truncated,
            )
        )
    return tuple(summaries)


def _prompt_budget(
    handoff: EvidenceAcquisitionHandoff,
    limits: PlanningLoopLimits,
    completed_turns: int,
) -> PromptBudgetRemaining:
    usage = handoff.budget_usage
    return PromptBudgetRemaining(
        collection_calls=usage.collection_limit - usage.collection_used,
        transform_calls=usage.transform_limit - usage.transform_used,
        planning_turns=limits.max_turns - completed_turns,
        calls_this_turn=limits.max_tool_calls_per_turn,
    )


def _tool_budget_exhausted(handoff: EvidenceAcquisitionHandoff) -> bool:
    usage = handoff.budget_usage
    return (
        usage.collection_used >= usage.collection_limit
        and usage.transform_used >= usage.transform_limit
    )


def _can_append_any_rule(scope, handoff: EvidenceAcquisitionHandoff) -> bool:
    existing = {
        (
            rule.output_parameter,
            rule.input_parameters,
            rule.temporal_relation,
            rule.spatial_relation,
            rule.maximum_inference_strength,
        )
        for rule in handoff.rule_plan.effective_rules
    }
    return any(
        (
            output,
            (input_parameter,),
            EvidenceTemporalRelation.SAME_WINDOW,
            EvidenceSpatialRelation.SAME_WATERBODY,
            EvidenceInferenceStrength.DIRECTIONAL,
        )
        not in existing
        for output in (*scope.requested_parameters, *scope.supporting_parameters)
        for input_parameter in (*scope.requested_parameters, *scope.supporting_parameters)
    )


def _planning_response_schema() -> WaterbodyJsonSchema:
    parameter = {
        "type": "object",
        "properties": {
            "code_system": {"type": "string", "minLength": 1},
            "code": {"type": "string", "minLength": 1},
        },
        "required": ["code_system", "code"],
        "additionalProperties": False,
    }
    rule = {
        "type": "object",
        "properties": {
            "output_parameter": parameter,
            "input_parameters": {
                "type": "array",
                "items": parameter,
                "minItems": 1,
                "uniqueItems": True,
            },
            "temporal_relation": {
                "type": "string",
                "enum": [item.value for item in EvidenceTemporalRelation],
            },
            "spatial_relation": {
                "type": "string",
                "enum": [item.value for item in EvidenceSpatialRelation],
            },
            "maximum_inference_strength": {
                "const": EvidenceInferenceStrength.DIRECTIONAL.value
            },
            "required_collection_attempt": {"const": False},
            "rationale": {"type": "string", "minLength": 1},
        },
        "required": [
            "output_parameter",
            "input_parameters",
            "temporal_relation",
            "spatial_relation",
            "maximum_inference_strength",
            "required_collection_attempt",
            "rationale",
        ],
        "additionalProperties": False,
    }
    return WaterbodyJsonSchema(
        name="evidence_planning_decision",
        schema_text=canonical_json_text(
            {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"decision": {"const": "evidence_ready"}},
                        "required": ["decision"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "decision": {"const": "evidence_rules_proposed"},
                            "rules": {
                                "type": "array",
                                "items": rule,
                                "minItems": 1,
                                "maxItems": _MAX_RULE_PROPOSALS_PER_TURN,
                            },
                        },
                        "required": ["decision", "rules"],
                        "additionalProperties": False,
                    },
                ]
            }
        ),
    )
