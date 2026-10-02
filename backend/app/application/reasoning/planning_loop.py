from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Callable, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator

from app.application.evidence_tools import (
    EvidenceAcquisitionHandoff,
    EvidenceAcquisitionService,
)
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRule,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
)
from app.application.reasoning.errors import PlanningLoopError
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyJsonSchema,
    WaterbodyModelMessage,
)
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.reasoning.prompt_builder import (
    EvidencePlanningPromptInput,
    PromptBudgetRemaining,
    PromptBuilder,
    WaterbodyStationKind,
    project_authorized_tool_roster,
)
from app.application.reasoning.prompt_policy import (
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.sufficiency import (
    ReasoningGateDecision,
    SealedEvidenceInput,
    seal_evidence_input,
)
from app.application.reasoning.task import ValidatedWaterbodyAgentTask
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    WorkingContextFact,
    WorkingContextFactKind,
    build_working_context_projection,
    require_safe_prompt_text,
)
from app.domain import (
    DomainModel,
    EvidenceToolOptions,
    EvidenceToolRequest,
    ItemReference,
    ModelUsageSummary,
    NonEmptyString,
    ParameterCapabilityRegistry,
    ParameterKey,
    SourceRecordReference,
)
from app.ports.waterbody_model import WaterbodyModelPort


_MAX_CONTEXT_FACTS = 120
_MAX_CONTEXT_SUMMARIES = 64
_MAX_RULE_PROPOSALS_PER_TURN = 4


class PlanningConvergenceReason(str, Enum):
    STATION_BACKED_EMPTY = "station_backed_empty"
    EVIDENCE_READY = "evidence_ready"
    NO_LEGAL_ACTIONS = "no_legal_actions"
    MAX_TURNS = "max_turns"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    DEADLINE_REACHED = "deadline_reached"


@dataclass(frozen=True, slots=True)
class PlanningLoopLimits:
    max_turns: int = 6
    max_tool_calls_per_turn: int = 4

    def __post_init__(self) -> None:
        if (
            type(self.max_turns) is not int
            or type(self.max_tool_calls_per_turn) is not int
            or self.max_turns <= 0
            or self.max_tool_calls_per_turn <= 0
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
    ) -> None:
        self._acquisition = acquisition
        self._capabilities = capability_registry
        self._prompt_builder = prompt_builder
        self._model_port = model_port
        self._limits = limits
        self._clock = clock or _UtcClock()
        self._operation_id_factory = operation_id_factory or uuid4

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline: datetime,
    ) -> EvidencePlanningOutcome:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("planning deadline must be timezone-aware")
        scope = task.task.evidence_scope
        handoff = await self._acquisition.start(scope)
        bundle_refs = [handoff.bundle_ref]
        preflight_calls = len(handoff.tool_result_refs)
        planning_calls = 0
        agent_tool_calls = 0
        rule_proposals = 0
        usage_input_tokens = 0
        usage_output_tokens = 0
        usage_reported = False
        convergence: PlanningConvergenceReason | None = None

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

            roster = self._acquisition.planning_roster(scope, handoff)
            if not roster and not _can_append_any_rule(scope, handoff):
                convergence = PlanningConvergenceReason.NO_LEGAL_ACTIONS
                break

            sealed = seal_evidence_input(task, handoff, self._capabilities)
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
                    gates=sealed.gates,
                    budget=_prompt_budget(handoff, self._limits, planning_calls),
                    authorized_tools=project_authorized_tool_roster(roster),
                    authorized_parameters=tuple(
                        dict.fromkeys(
                            (
                                *scope.requested_parameters,
                                *scope.supporting_parameters,
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

            if tool_events:
                for event in tool_events:
                    if self._clock.now() >= deadline:
                        convergence = PlanningConvergenceReason.DEADLINE_REACHED
                        break
                    request = await _tool_request(scope, handoff, event)
                    previous_results = len(handoff.tool_result_refs)
                    handoff = await self._acquisition.execute_optional(
                        scope,
                        (request,),
                        handoff,
                        max_calls=self._limits.max_tool_calls_per_turn,
                    )
                    agent_tool_calls += (
                        len(handoff.tool_result_refs) - previous_results
                    )
                    bundle_refs.append(handoff.bundle_ref)
                    if scope.cancel_token.is_requested():
                        raise PlanningLoopError("planning_cancelled")

            if convergence is PlanningConvergenceReason.DEADLINE_REACHED:
                continue

            if isinstance(decision, _EvidenceRulesDecision):
                try:
                    rules = tuple(
                        _materialize_rule(scope.session_id, proposal)
                        for proposal in decision.rules
                    )
                except ValueError as error:
                    raise PlanningLoopError("invalid_planning_response") from error
                previous_count = len(handoff.rule_plan.appended_rules)
                handoff = await self._acquisition.append_rules(
                    scope,
                    rules,
                    handoff,
                )
                rule_proposals += (
                    len(handoff.rule_plan.appended_rules) - previous_count
                )
                bundle_refs.append(handoff.bundle_ref)
            elif isinstance(decision, _EvidenceReadyDecision):
                convergence = PlanningConvergenceReason.EVIDENCE_READY

        if convergence is None:
            convergence = PlanningConvergenceReason.MAX_TURNS

        handoff = self._acquisition.seal(scope, handoff)
        sealed = seal_evidence_input(task, handoff, self._capabilities)
        return EvidencePlanningOutcome(
            sealed_input=sealed,
            handoff=handoff,
            counts=EvidencePlanningStageCounts(
                preflight_tool_calls=preflight_calls,
                planning_model_calls=planning_calls,
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


async def _tool_request(
    scope,
    handoff: EvidenceAcquisitionHandoff,
    event: ToolCallCompleted,
) -> EvidenceToolRequest:
    try:
        payload = json.loads(event.arguments)
        if set(payload) != {"window_ids", "parameter_keys", "options"}:
            raise ValueError
        options = dict(payload["options"])
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


async def _working_context(
    task: ValidatedWaterbodyAgentTask,
    handoff: EvidenceAcquisitionHandoff,
    sealed: SealedEvidenceInput,
):
    facts: list[WorkingContextFact] = []
    for gate in sealed.gates[:_MAX_CONTEXT_FACTS]:
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
                refs=tuple(
                    dict.fromkeys(
                        (
                            *gate.allowed_evidence_refs,
                            *gate.contextual_refs,
                            *gate.collection_fact_refs,
                            *gate.blocker_refs,
                        )
                    )
                ),
            )
        )

    summaries = []
    for reference in sorted(
        handoff.fragment_allowlist,
        key=lambda item: (item.evidence_id, item.item_id),
    )[:_MAX_CONTEXT_SUMMARIES]:
        fragment = await handoff.read_port.read_fragment(
            task.task.evidence_scope,
            reference,
        )
        summary = canonical_json_text(
            {
                "data_type": fragment.data_type,
                "collection_status": fragment.collection_status,
                "applicability": fragment.applicability,
                "quality_class": fragment.quality_class,
                "coverage_status": fragment.coverage.status,
                "parameters": fragment.parameter_refs,
                "relation_id": fragment.relation_id,
            }
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
                series_refs=(
                    (fragment.series_identity,)
                    if fragment.series_identity is not None
                    else ()
                ),
                truncated=False,
            )
        )

    scope = task.task.evidence_scope
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
    allowlist = tuple(
        dict.fromkeys(
            (
                *(reference.evidence_id for reference in handoff.fragment_allowlist),
                *handoff.source_record_allowlist,
            )
        )
    )
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
        facts=tuple(facts),
        evidence_summaries=tuple(summaries),
    )


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
