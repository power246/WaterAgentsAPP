from __future__ import annotations

import json
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from app.application.evidence_tools import EvidenceToolDescriptor
from app.application.reasoning.model_models import (
    ModelItemName,
    NonBlankText,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
    WaterbodyToolDescriptor,
)
from app.application.reasoning.prompt_policy import (
    PromptAuditFailure,
    ReasoningPromptPolicy,
    audit_prompt_policy,
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.result_models import AgentReasoningCellScope
from app.application.reasoning.sufficiency import ReasoningCellGate
from app.application.reasoning.working_context import (
    WorkingContextFactKind,
    WorkingContextProjection,
    require_safe_prompt_payload,
    require_safe_prompt_text,
)
from app.domain import (
    DomainEnum,
    DomainFloat,
    DomainModel,
    ModelSelection,
    NonEmptyString,
    OutcomeStatus,
    ParameterKey,
    RiskLevel,
    UtcDatetime,
    ensure_unique_tuple,
)


class WaterbodyStationKind(DomainEnum):
    STATION_BACKED = "station_backed"
    STATIONLESS = "stationless"


class PromptBudgetRemaining(DomainModel):
    collection_calls: Annotated[int, Field(strict=True, ge=0)]
    transform_calls: Annotated[int, Field(strict=True, ge=0)]
    planning_turns: Annotated[int, Field(strict=True, ge=0)]
    calls_this_turn: Annotated[int, Field(strict=True, ge=0)]


class PromptToolAuthorization(DomainModel):
    name: ModelItemName
    purpose: NonEmptyString
    policy_class: NonEmptyString
    allowed_option_fields: tuple[NonEmptyString, ...]
    allowed_aggregation_methods: tuple[NonEmptyString, ...]
    allowed_target_unit_ids: tuple[NonEmptyString, ...]
    max_calls_per_session: Annotated[int, Field(strict=True, ge=0)]
    max_records_per_call: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @model_validator(mode="after")
    def validate_authorization(self) -> Self:
        for values in (
            self.allowed_option_fields,
            self.allowed_aggregation_methods,
            self.allowed_target_unit_ids,
        ):
            ensure_unique_tuple(values, field_name="tool authorization values")
        return self


def project_authorized_tool_roster(
    descriptors: tuple[EvidenceToolDescriptor, ...],
) -> tuple[PromptToolAuthorization, ...]:
    names = tuple(descriptor.name for descriptor in descriptors)
    ensure_unique_tuple(names, field_name="authorized prompt tool roster")
    return tuple(
        PromptToolAuthorization(
            name=descriptor.name,
            purpose=descriptor.purpose,
            policy_class=descriptor.policy_class.value,
            allowed_option_fields=descriptor.allowed_option_fields,
            allowed_aggregation_methods=descriptor.allowed_aggregation_methods,
            allowed_target_unit_ids=descriptor.allowed_target_unit_ids,
            max_calls_per_session=descriptor.max_calls_per_session,
            max_records_per_call=descriptor.max_records_per_call,
        )
        for descriptor in descriptors
    )


class _PromptInput(DomainModel):
    operation_id: UUID
    model: ModelSelection
    deadline: UtcDatetime
    context: WorkingContextProjection
    response_schema: WaterbodyJsonSchema


class EvidencePlanningPromptInput(_PromptInput):
    station_kind: WaterbodyStationKind
    gates: tuple[ReasoningCellGate, ...]
    budget: PromptBudgetRemaining
    authorized_tools: tuple[PromptToolAuthorization, ...]
    authorized_parameters: tuple[ParameterKey, ...] = ()

    @model_validator(mode="after")
    def validate_planning_scope(self) -> Self:
        if not self.gates:
            raise ValueError("evidence planning requires at least one gate")
        ensure_unique_tuple(
            tuple(gate.cell_id for gate in self.gates),
            field_name="evidence planning gate cells",
        )
        ensure_unique_tuple(
            tuple(tool.name for tool in self.authorized_tools),
            field_name="evidence planning tool names",
        )
        ensure_unique_tuple(
            self.authorized_parameters,
            field_name="evidence planning parameters",
        )
        allowed = set(self.context.reference_allowlist)
        refs = {
            ref
            for gate in self.gates
            for ref in (
                *gate.allowed_evidence_refs,
                *gate.contextual_refs,
                *gate.collection_fact_refs,
                *gate.blocker_refs,
            )
        }
        if not refs <= allowed:
            raise ValueError("planning gate refs must be in the working-context allowlist")
        for gate in self.gates:
            for reason in gate.reasons:
                require_safe_prompt_text(reason)
        return self


class ReasoningBatchPromptInput(_PromptInput):
    batch_id: NonEmptyString
    cells: tuple[AgentReasoningCellScope, ...]

    @model_validator(mode="after")
    def validate_batch_scope(self) -> Self:
        if not self.cells or len(self.cells) > 32:
            raise ValueError("reasoning batch must contain between 1 and 32 cells")
        ensure_unique_tuple(
            tuple(cell.cell_id for cell in self.cells),
            field_name="reasoning batch cell IDs",
        )
        allowed_refs = {
            ref for cell in self.cells for ref in cell.allowed_evidence_refs
        }
        context_refs = {
            summary.evidence_ref for summary in self.context.evidence_summaries
        }
        if not context_refs <= allowed_refs:
            raise ValueError("working context contains evidence outside batch evidence scope")
        return self


class ValidatedCellPromptSummary(DomainModel):
    cell_id: NonEmptyString
    window_id: NonEmptyString
    outcome_status: OutcomeStatus
    risk_level: RiskLevel
    risk_rationale: NonBlankText
    confidence: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
    confidence_rationale: NonBlankText
    evidence_refs: tuple[NonEmptyString, ...]

    @field_validator("risk_rationale", "confidence_rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @model_validator(mode="after")
    def validate_refs(self) -> Self:
        ensure_unique_tuple(self.evidence_refs, field_name="validated-cell evidence refs")
        return self


class FixedCellPromptSummary(DomainModel):
    cell_id: NonEmptyString
    window_id: NonEmptyString
    reasons: tuple[NonEmptyString, ...]
    blocker_refs: tuple[NonEmptyString, ...]

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for reason in value:
            require_safe_prompt_text(reason)
        return value

    @model_validator(mode="after")
    def validate_fixed_cell(self) -> Self:
        if not self.reasons:
            raise ValueError("fixed cell requires at least one reason")
        ensure_unique_tuple(self.reasons, field_name="fixed-cell reasons")
        ensure_unique_tuple(self.blocker_refs, field_name="fixed-cell blocker refs")
        return self


class OverallSynthesisPromptInput(_PromptInput):
    validated_cells: tuple[ValidatedCellPromptSummary, ...]
    fixed_cells: tuple[FixedCellPromptSummary, ...]

    @model_validator(mode="after")
    def validate_synthesis_scope(self) -> Self:
        identities = tuple(
            cell.cell_id for cell in (*self.validated_cells, *self.fixed_cells)
        )
        if not identities:
            raise ValueError("overall synthesis requires at least one cell")
        ensure_unique_tuple(identities, field_name="synthesis cell IDs")
        refs = {
            ref
            for cell in self.validated_cells
            for ref in cell.evidence_refs
        } | {
            ref for cell in self.fixed_cells for ref in cell.blocker_refs
        }
        if not refs <= set(self.context.reference_allowlist):
            raise ValueError("synthesis refs must be in the working-context allowlist")
        return self


class PromptValidationIssue(DomainModel):
    code: ModelItemName
    path: NonEmptyString

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return require_safe_prompt_text(value)


class SchemaRepairPromptInput(_PromptInput):
    repair_of_operation_id: UUID
    original_phase: WaterbodyModelPhase
    batch_id: NonEmptyString | None = None
    original_visible_response: NonBlankText
    issues: tuple[PromptValidationIssue, ...]

    @field_validator("original_visible_response")
    @classmethod
    def validate_visible_response(cls, value: str) -> str:
        if len(value) > 131_072:
            raise ValueError("schema-repair response exceeds the bounded character limit")
        return require_safe_prompt_payload(value)

    @model_validator(mode="after")
    def validate_repair_scope(self) -> Self:
        if self.original_phase is WaterbodyModelPhase.SCHEMA_REPAIR:
            raise ValueError("schema repair cannot recursively repair schema repair")
        if not self.issues or len(self.issues) > 64:
            raise ValueError("schema repair requires between 1 and 64 issues")
        if (
            self.original_phase is WaterbodyModelPhase.REASONING_BATCH
        ) != (self.batch_id is not None):
            raise ValueError("reasoning-batch repair requires exactly one batch identity")
        return self


class PromptPhaseAudit(DomainModel):
    phase: WaterbodyModelPhase
    tool_names: tuple[ModelItemName, ...]
    schema_digest: NonEmptyString
    prompt_digest: NonEmptyString


class PromptContractAudit(DomainModel):
    rule_count: StrictInt
    policy_digest: NonEmptyString
    phases: tuple[PromptPhaseAudit, ...]
    failures: tuple[PromptAuditFailure, ...]


class PromptBuilder:
    def __init__(self, policy: ReasoningPromptPolicy) -> None:
        report = audit_prompt_policy(policy)
        if report.failures:
            raise ValueError("prompt policy audit must pass before rendering")
        self._policy = policy

    def build_evidence_planning(
        self,
        prompt: EvidencePlanningPromptInput,
    ) -> WaterbodyModelRequest:
        tool_descriptors = tuple(
            self._build_tool_descriptor(tool, prompt)
            for tool in prompt.authorized_tools
        )
        body = {
            "phase": WaterbodyModelPhase.EVIDENCE_PLANNING,
            "station_kind": prompt.station_kind,
            "scope_digest": prompt.context.scope_digest,
            "bundle_ref": prompt.context.bundle_ref,
            "bundle_digest": prompt.context.bundle_digest,
            "rule_plan_digest": prompt.context.rule_plan_digest,
            "capability_registry_digest": prompt.context.capability_registry_digest,
            "working_context_digest": prompt.context.working_context_digest,
            "gates": prompt.gates,
            "missing_facts": tuple(
                fact
                for fact in prompt.context.facts
                if fact.kind is WorkingContextFactKind.MISSING
            ),
            "blocker_facts": tuple(
                fact
                for fact in prompt.context.facts
                if fact.kind is WorkingContextFactKind.BLOCKER
            ),
            "coverage_facts": tuple(
                fact
                for fact in prompt.context.facts
                if fact.kind is WorkingContextFactKind.COVERAGE
            ),
            "evidence_summaries": prompt.context.evidence_summaries,
            "budget": prompt.budget,
            "authorized_tools": prompt.authorized_tools,
        }
        return self._request(
            phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
            prompt=prompt,
            body=body,
            tools=tool_descriptors,
        )

    def build_reasoning_batch(
        self,
        prompt: ReasoningBatchPromptInput,
    ) -> WaterbodyModelRequest:
        allowed = {ref for cell in prompt.cells for ref in cell.allowed_evidence_refs}
        body = {
            "phase": WaterbodyModelPhase.REASONING_BATCH,
            "batch_id": prompt.batch_id,
            "scope_digest": prompt.context.scope_digest,
            "bundle_ref": prompt.context.bundle_ref,
            "bundle_digest": prompt.context.bundle_digest,
            "rule_plan_digest": prompt.context.rule_plan_digest,
            "capability_registry_digest": prompt.context.capability_registry_digest,
            "working_context_digest": prompt.context.working_context_digest,
            "cells": prompt.cells,
            "evidence_summaries": tuple(
                summary
                for summary in prompt.context.evidence_summaries
                if summary.evidence_ref in allowed
            ),
            "response_schema": {
                "name": prompt.response_schema.name,
                "digest": _schema_digest(prompt.response_schema),
            },
        }
        return self._request(
            phase=WaterbodyModelPhase.REASONING_BATCH,
            prompt=prompt,
            body=body,
            batch_id=prompt.batch_id,
        )

    def build_overall_synthesis(
        self,
        prompt: OverallSynthesisPromptInput,
    ) -> WaterbodyModelRequest:
        body = {
            "phase": WaterbodyModelPhase.OVERALL_SYNTHESIS,
            "scope_digest": prompt.context.scope_digest,
            "bundle_ref": prompt.context.bundle_ref,
            "bundle_digest": prompt.context.bundle_digest,
            "working_context_digest": prompt.context.working_context_digest,
            "validated_cells": prompt.validated_cells,
            "fixed_cells": prompt.fixed_cells,
            "coverage_facts": tuple(
                fact
                for fact in prompt.context.facts
                if fact.kind
                in {
                    WorkingContextFactKind.COVERAGE,
                    WorkingContextFactKind.MISSING,
                    WorkingContextFactKind.BLOCKER,
                }
            ),
            # The synthesis context is already server-bounded to this skeleton.
            # Keeping its contextual summaries lets the Agent propose an explicitly
            # hypothesized attribution without allowing those refs to unlock a cell.
            "bounded_reference_summaries": prompt.context.evidence_summaries,
        }
        return self._request(
            phase=WaterbodyModelPhase.OVERALL_SYNTHESIS,
            prompt=prompt,
            body=body,
        )

    def build_schema_repair(
        self,
        prompt: SchemaRepairPromptInput,
    ) -> WaterbodyModelRequest:
        body = {
            "phase": WaterbodyModelPhase.SCHEMA_REPAIR,
            "repair_of_operation_id": prompt.repair_of_operation_id,
            "original_phase": prompt.original_phase,
            "batch_id": prompt.batch_id,
            "scope_digest": prompt.context.scope_digest,
            "bundle_ref": prompt.context.bundle_ref,
            "bundle_digest": prompt.context.bundle_digest,
            "response_schema": {
                "name": prompt.response_schema.name,
                "digest": _schema_digest(prompt.response_schema),
            },
            "original_visible_response": prompt.original_visible_response,
            "issues": prompt.issues,
        }
        return self._request(
            phase=WaterbodyModelPhase.SCHEMA_REPAIR,
            prompt=prompt,
            body=body,
            batch_id=prompt.batch_id,
            include_recent_messages=False,
        )

    def _request(
        self,
        *,
        phase: WaterbodyModelPhase,
        prompt: _PromptInput,
        body: object,
        tools: tuple[WaterbodyToolDescriptor, ...] = (),
        batch_id: str | None = None,
        include_recent_messages: bool = True,
    ) -> WaterbodyModelRequest:
        recent = prompt.context.recent_messages if include_recent_messages else ()
        body_text = require_safe_prompt_payload(canonical_json_text(body))
        messages = (
            *recent,
            WaterbodyModelMessage(
                message_id=f"m6:{prompt.operation_id}:{phase.value}:input",
                role=WaterbodyMessageRole.USER,
                text=body_text,
            ),
        )
        return WaterbodyModelRequest(
            operation_id=prompt.operation_id,
            phase=phase,
            model=prompt.model,
            system_prompt=self._render_system_prompt(phase),
            messages=messages,
            tool_descriptors=tools,
            response_schema=prompt.response_schema,
            prompt_policy_digest=self._policy.policy_digest,
            scope_digest=prompt.context.scope_digest,
            evidence_bundle_ref=prompt.context.bundle_ref,
            batch_id=batch_id,
            deadline=prompt.deadline,
        )

    def _render_system_prompt(self, phase: WaterbodyModelPhase) -> str:
        permission = next(
            item for item in self._policy.phase_permissions if item.phase is phase
        )
        lines = [
            "Waterbody reasoning contract. Follow every bracketed rule exactly.",
            f"[phase] {phase.value}",
        ]
        lines.extend(
            f"[{rule.rule_id}] {rule.instruction}"
            for rule in self._policy.rules
            if phase in rule.phases
        )
        lines.extend(
            (
                f"[allowed_actions] {', '.join(permission.allowed_actions)}",
                f"[forbidden_actions] {', '.join(permission.forbidden_actions)}",
                f"[tools_allowed] {'true' if permission.tools_allowed else 'false'}",
            )
        )
        return "\n".join(lines)

    def _build_tool_descriptor(
        self,
        tool: PromptToolAuthorization,
        prompt: EvidencePlanningPromptInput,
    ) -> WaterbodyToolDescriptor:
        options = {
            field: _option_schema(field, tool, prompt.context)
            for field in tool.allowed_option_fields
        }
        window_ids = tuple(dict.fromkeys(gate.window_id for gate in prompt.gates))
        parameter_keys = tuple(
            (parameter.code_system, parameter.code)
            for parameter in (
                prompt.authorized_parameters
                or tuple(dict.fromkeys(gate.parameter for gate in prompt.gates))
            )
        )
        schema = {
            "type": "object",
            "properties": {
                "window_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": window_ids},
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "parameter_keys": {
                    "type": "array",
                    "items": {
                        "oneOf": tuple(
                            {
                                "type": "object",
                                "properties": {
                                    "code_system": {"const": code_system},
                                    "code": {"const": code},
                                },
                                "required": ["code_system", "code"],
                                "additionalProperties": False,
                            }
                            for code_system, code in parameter_keys
                        ),
                    },
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "options": {
                    "type": "object",
                    "properties": options,
                    "additionalProperties": False,
                },
            },
            "required": ["window_ids", "parameter_keys", "options"],
            "additionalProperties": False,
        }
        return WaterbodyToolDescriptor(
            name=tool.name,
            description=tool.purpose,
            input_schema=WaterbodyJsonSchema(
                name=tool.name,
                schema_text=canonical_json_text(schema),
            ),
        )


def audit_prompt_contract(
    policy: ReasoningPromptPolicy,
    requests: tuple[WaterbodyModelRequest, ...],
) -> PromptContractAudit:
    policy_report = audit_prompt_policy(policy)
    failures = list(policy_report.failures)
    by_phase: dict[WaterbodyModelPhase, WaterbodyModelRequest] = {}
    for request in requests:
        if request.phase in by_phase:
            failures.append(
                PromptAuditFailure(
                    code="duplicate_phase_request",
                    phase=request.phase,
                    detail="More than one request was supplied for the phase.",
                )
            )
        by_phase[request.phase] = request
    phases: list[PromptPhaseAudit] = []
    permissions = {item.phase: item for item in policy.phase_permissions}
    for phase in WaterbodyModelPhase:
        request = by_phase.get(phase)
        if request is None:
            failures.append(
                PromptAuditFailure(
                    code="missing_phase_request",
                    phase=phase,
                    detail="No request was supplied for the phase.",
                )
            )
            continue
        if request.prompt_policy_digest != policy.policy_digest:
            failures.append(
                PromptAuditFailure(
                    code="prompt_policy_digest_mismatch",
                    phase=phase,
                    detail="Request policy digest differs from the audited policy.",
                )
            )
        permission = permissions[phase]
        if bool(request.tool_descriptors) is not permission.tools_allowed:
            failures.append(
                PromptAuditFailure(
                    code="phase_tool_roster_mismatch",
                    phase=phase,
                    detail="Request tool roster violates phase permissions.",
                )
            )
        for rule in policy.rules:
            if phase in rule.phases and f"[{rule.rule_id}]" not in request.system_prompt:
                failures.append(
                    PromptAuditFailure(
                        code="rendered_rule_missing",
                        phase=phase,
                        detail=f"Rendered system prompt omits {rule.rule_id}.",
                    )
                )
        phases.append(
            PromptPhaseAudit(
                phase=phase,
                tool_names=tuple(tool.name for tool in request.tool_descriptors),
                schema_digest=_schema_digest(request.response_schema),
                prompt_digest=canonical_json_digest(
                    {
                        "system_prompt": request.system_prompt,
                        "messages": request.messages,
                        "tools": request.tool_descriptors,
                        "response_schema": request.response_schema,
                        "scope_digest": request.scope_digest,
                        "batch_id": request.batch_id,
                    }
                ),
            )
        )
    return PromptContractAudit(
        rule_count=len(policy.rules),
        policy_digest=policy.policy_digest,
        phases=tuple(phases),
        failures=tuple(failures),
    )


def _option_schema(
    field: str,
    tool: PromptToolAuthorization,
    context: WorkingContextProjection,
) -> dict[str, object]:
    source_refs = tuple(
        sorted(
            {
                ref
                for summary in context.evidence_summaries
                for ref in summary.source_record_item_refs
            }
        )
    )
    evidence_refs = tuple(
        summary.evidence_ref for summary in context.evidence_summaries
    )
    if field == "include_forecast":
        return {"type": "boolean"}
    if field == "facility_radius_miles":
        return {"type": "number", "minimum": 1, "maximum": 20}
    if field == "record_limit":
        return {
            "type": "integer",
            "minimum": 1,
            "maximum": tool.max_records_per_call,
        }
    if field == "fragment_refs":
        return {
            "type": "array",
            "items": {"type": "string", "enum": evidence_refs},
            "minItems": 1,
            "uniqueItems": True,
        }
    if field == "target_unit_id":
        return {"type": "string", "enum": tool.allowed_target_unit_ids}
    if field == "source_record_refs":
        return {
            "type": "array",
            "items": {"type": "string", "enum": source_refs},
            "minItems": 1,
            "uniqueItems": True,
        }
    if field == "aggregation_method":
        return {"type": "string", "enum": tool.allowed_aggregation_methods}
    if field == "source_item_id":
        return {"type": "string", "enum": source_refs}
    if field == "page_offset":
        return {"type": "integer", "minimum": 0}
    raise ValueError(f"unsupported prompt tool option field: {field}")


def _schema_digest(schema: WaterbodyJsonSchema) -> str:
    return canonical_json_digest(json.loads(schema.schema_text))
