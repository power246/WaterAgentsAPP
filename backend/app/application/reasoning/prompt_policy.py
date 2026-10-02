from __future__ import annotations

import json
from enum import Enum
from hashlib import sha256
from typing import Self
from uuid import UUID

from pydantic import StrictBool, field_validator, model_validator

from app.application.reasoning.model_models import (
    ModelItemName,
    NonBlankText,
    WaterbodyModelPhase,
)
from app.domain import DomainModel, NonEmptyString, ensure_unique_tuple


def canonical_json_text(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_digest(value: object) -> str:
    return f"sha256:{sha256(canonical_json_text(value).encode('utf-8')).hexdigest()}"


class PromptPolicyRule(DomainModel):
    rule_id: ModelItemName
    phases: tuple[WaterbodyModelPhase, ...]
    instruction: NonBlankText
    applies_to_all_phases: StrictBool = False

    @model_validator(mode="after")
    def validate_phases(self) -> Self:
        if not self.phases:
            raise ValueError("prompt policy rule requires at least one phase")
        ensure_unique_tuple(self.phases, field_name="prompt policy rule phases")
        if self.applies_to_all_phases and set(self.phases) != set(WaterbodyModelPhase):
            raise ValueError("shared prompt policy rule must cover every phase")
        return self


class PromptPhasePermission(DomainModel):
    phase: WaterbodyModelPhase
    tools_allowed: StrictBool
    allowed_actions: tuple[NonEmptyString, ...]
    forbidden_actions: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_actions(self) -> Self:
        if not self.allowed_actions or not self.forbidden_actions:
            raise ValueError("phase permission requires allowed and forbidden actions")
        ensure_unique_tuple(self.allowed_actions, field_name="allowed prompt actions")
        ensure_unique_tuple(self.forbidden_actions, field_name="forbidden prompt actions")
        return self


class PromptPolicyOwnership(DomainModel):
    server_owned_fields: tuple[NonEmptyString, ...]
    agent_owned_fields: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        ensure_unique_tuple(self.server_owned_fields, field_name="server-owned fields")
        ensure_unique_tuple(self.agent_owned_fields, field_name="agent-owned fields")
        if set(self.server_owned_fields) & set(self.agent_owned_fields):
            raise ValueError("server-owned and agent-owned fields must be disjoint")
        return self


class ReasoningPromptPolicy(DomainModel):
    schema_version: int = 1
    rules: tuple[PromptPolicyRule, ...]
    phase_permissions: tuple[PromptPhasePermission, ...]
    ownership: PromptPolicyOwnership
    policy_digest: NonEmptyString

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("prompt policy schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        ensure_unique_tuple(
            tuple(rule.rule_id for rule in self.rules),
            field_name="prompt policy rule IDs",
        )
        ensure_unique_tuple(
            tuple(permission.phase for permission in self.phase_permissions),
            field_name="prompt phase permissions",
        )
        if self.policy_digest != canonical_json_digest(
            self.model_dump(mode="json", exclude={"policy_digest"})
        ):
            raise ValueError("prompt policy digest does not match canonical policy")
        return self


class PromptAuditFailure(DomainModel):
    code: ModelItemName
    phase: WaterbodyModelPhase | None = None
    detail: NonEmptyString


class PromptPolicyAuditReport(DomainModel):
    rule_count: int
    policy_digest: NonEmptyString
    failures: tuple[PromptAuditFailure, ...]


_REQUIRED_BASE_RULE_IDS = frozenset(
    {
        "absence_is_not_none",
        "attribution_eligibility",
        "confidence_semantics",
        "evidence_eligibility",
        "outcome_semantics",
        "reference_integrity",
        "risk_levels",
        "risk_rationale",
        "secret_safety",
        "series_review",
        "source_eligibility",
        "unit_and_aggregation",
        "visible_reasoning_only",
    }
)


def audit_prompt_policy(policy: ReasoningPromptPolicy) -> PromptPolicyAuditReport:
    failures: list[PromptAuditFailure] = []
    base_rule_ids = {
        rule.rule_id for rule in policy.rules if rule.applies_to_all_phases
    }
    missing = sorted(_REQUIRED_BASE_RULE_IDS - base_rule_ids)
    for rule_id in missing:
        failures.append(
            PromptAuditFailure(
                code="missing_base_rule",
                detail=f"Required shared rule is missing: {rule_id}",
            )
        )
    instructions = {rule.rule_id: rule.instruction.lower() for rule in policy.rules}
    required_anchors = {
        "risk_levels": ("none", "low", "medium", "high", "indeterminate"),
        "risk_rationale": ("evidence", "interpretation", "level"),
        "confidence_semantics": (
            "confidence",
            "risk",
            "quality",
            "coverage",
            "cell",
            "series",
            "window",
            "range",
        ),
        "series_review": (
            "station",
            "time",
            "medium",
            "method",
            "statistic",
            "quality",
            "coverage",
            "suspicious",
        ),
        "source_eligibility": ("observed", "inferred", "predicted"),
        "unit_and_aggregation": ("deterministic", "conversion", "aggregation"),
        "evidence_eligibility": ("gate", "allowed evidence"),
        "secret_safety": ("credentials", "provider", "url"),
        "visible_reasoning_only": ("visible", "concealed"),
        "absence_is_not_none": ("no evidence", "indeterminate", "none"),
        "outcome_semantics": (
            "quantified",
            "directional",
            "indeterminate",
            "availability",
        ),
        "attribution_eligibility": (
            "observed",
            "correlated",
            "inferred",
            "hypothesized",
        ),
    }
    for rule_id, anchors in required_anchors.items():
        instruction = instructions.get(rule_id, "")
        absent = tuple(anchor for anchor in anchors if anchor not in instruction)
        if absent:
            failures.append(
                PromptAuditFailure(
                    code="rule_semantics_incomplete",
                    detail=f"Rule {rule_id} is missing anchors: {', '.join(absent)}",
                )
            )
    rendered_policy = "\n".join(instructions.values())
    for forbidden in (
        "0-100 score",
        "take the maximum risk",
        "average risk levels",
    ):
        if forbidden in rendered_policy:
            failures.append(
                PromptAuditFailure(
                    code="policy_conflict",
                    detail=f"Policy contains forbidden scoring instruction: {forbidden}",
                )
            )
    permissions = {permission.phase: permission for permission in policy.phase_permissions}
    for phase in WaterbodyModelPhase:
        permission = permissions.get(phase)
        if permission is None:
            failures.append(
                PromptAuditFailure(
                    code="missing_phase_permission",
                    phase=phase,
                    detail="Phase permission is missing.",
                )
            )
            continue
        expected_tools = phase is WaterbodyModelPhase.EVIDENCE_PLANNING
        if permission.tools_allowed is not expected_tools:
            failures.append(
                PromptAuditFailure(
                    code="invalid_tool_permission",
                    phase=phase,
                    detail="Only evidence planning may expose tools.",
                )
            )
        if not any(
            phase in rule.phases and not rule.applies_to_all_phases
            for rule in policy.rules
        ):
            failures.append(
                PromptAuditFailure(
                    code="missing_phase_rule",
                    phase=phase,
                    detail="Phase-specific rule is missing.",
                )
            )
    if set(policy.ownership.server_owned_fields) & set(
        policy.ownership.agent_owned_fields
    ):
        failures.append(
            PromptAuditFailure(
                code="ownership_conflict",
                detail="Server-owned and agent-owned fields overlap.",
            )
        )
    return PromptPolicyAuditReport(
        rule_count=len(policy.rules),
        policy_digest=policy.policy_digest,
        failures=tuple(failures),
    )


def _rule(
    rule_id: str,
    instruction: str,
    *,
    phases: tuple[WaterbodyModelPhase, ...] | None = None,
) -> PromptPolicyRule:
    selected = phases or tuple(WaterbodyModelPhase)
    return PromptPolicyRule(
        rule_id=rule_id,
        phases=selected,
        instruction=instruction,
        applies_to_all_phases=phases is None,
    )


def _build_default_policy() -> ReasoningPromptPolicy:
    rules = (
        _rule(
            "evidence_eligibility",
            "Honor every server evidence gate. Use only allowed evidence references; contextual and collection facts explain coverage but are not measurement evidence.",
        ),
        _rule(
            "source_eligibility",
            "Use observed only for one direct current source record. Mark aggregates, cross-series selection, upstream interpretation, and gap fill inferred; forecasts are predicted and indeterminate has no source.",
        ),
        _rule(
            "outcome_semantics",
            "Choose quantified only for an eligible supported value, directional only for an eligible supported trend, and indeterminate otherwise. Outcome, availability, confidence, and risk are separate meanings.",
        ),
        _rule(
            "risk_levels",
            "Risk level must be exactly one of none, low, medium, high, or indeterminate. Do not invent numeric thresholds.",
        ),
        _rule(
            "absence_is_not_none",
            "No evidence, unavailable evidence, or insufficient coverage is indeterminate; it never establishes a none risk level.",
        ),
        _rule(
            "risk_rationale",
            "For each risk conclusion, state the eligible evidence first, then the interpretation, then the selected risk level.",
        ),
        _rule(
            "confidence_semantics",
            "Confidence is separate from risk, outcome, and availability. At every applicable cell, series, window, and range layer, explain confidence using source directness, quality, coverage, consistency, matching, inference span, and blockers.",
        ),
        _rule(
            "series_review",
            "Read each series separately. Before comparing, confirm station, time window, medium or fraction, method, statistic, value semantics, quality, and coverage; retain important opposing records. Suspicious values remain visible with their quality flag unless invalid, and incompatible series are not silently deleted or averaged.",
        ),
        _rule(
            "unit_and_aggregation",
            "Use only deterministic authorized conversion and aggregation outputs. Never perform or imply a new unit conversion or aggregate calculation in prose.",
        ),
        _rule(
            "reference_integrity",
            "Cite only stable references present in the bounded working context and never create an evidence, record, station, waterbody, or tool reference.",
        ),
        _rule(
            "attribution_eligibility",
            "Attribution basis is observed only for a direct record, correlated only for a registered allowed-indirect relation, inferred for supported derivation, and hypothesized for contextual explanation. Every factor cites eligible evidence and hypothesized factors are not proven causes.",
        ),
        _rule(
            "scope_integrity",
            "Stay within the supplied waterbody, windows, parameters, batch cells, and server-owned scope digests.",
        ),
        _rule(
            "secret_safety",
            "Never request, repeat, infer, or expose credentials, provider routing, URLs, authorization material, or internal transport payloads.",
        ),
        _rule(
            "visible_reasoning_only",
            "Return concise visible rationales and structured results only. Do not request, expose, or claim concealed reasoning.",
        ),
        _rule(
            "planning_contract",
            "Select only from the server-authorized tool roster and bounded options. You may plan evidence collection but may not choose provider, COMID, station, URL, credential, or arbitrary identifiers.",
            phases=(WaterbodyModelPhase.EVIDENCE_PLANNING,),
        ),
        _rule(
            "batch_contract",
            "Reason only over the supplied batch cells and their allowed evidence references. Produce the exact response schema without tools or external facts.",
            phases=(WaterbodyModelPhase.REASONING_BATCH,),
        ),
        _rule(
            "synthesis_contract",
            "Synthesize all validated and fixed cells, coverage facts, and bounded references across windows. Preserve four-layer confidence and explicit AnalysisDetail and AttributionSummary-compatible rationales without raw records.",
            phases=(WaterbodyModelPhase.OVERALL_SYNTHESIS,),
        ),
        _rule(
            "repair_contract",
            "Mechanically repair only the listed validation issues against the original schema and unchanged scope. Use the configured model identity; add no tools, evidence, facts, or open-ended analysis.",
            phases=(WaterbodyModelPhase.SCHEMA_REPAIR,),
        ),
    )
    permissions = (
        PromptPhasePermission(
            phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
            tools_allowed=True,
            allowed_actions=("select_authorized_tools", "declare_evidence_ready"),
            forbidden_actions=("select_provider", "invent_scope_identity"),
        ),
        PromptPhasePermission(
            phase=WaterbodyModelPhase.REASONING_BATCH,
            tools_allowed=False,
            allowed_actions=("reason_over_batch", "emit_cell_results"),
            forbidden_actions=("call_tools", "use_outside_evidence"),
        ),
        PromptPhasePermission(
            phase=WaterbodyModelPhase.OVERALL_SYNTHESIS,
            tools_allowed=False,
            allowed_actions=("synthesize_validated_cells", "explain_coverage"),
            forbidden_actions=("call_tools", "recompute_results"),
        ),
        PromptPhasePermission(
            phase=WaterbodyModelPhase.SCHEMA_REPAIR,
            tools_allowed=False,
            allowed_actions=("repair_schema_shape",),
            forbidden_actions=("call_tools", "add_evidence", "change_scope"),
        ),
    )
    ownership = PromptPolicyOwnership(
        server_owned_fields=(
            "provider",
            "model",
            "target_comid",
            "station_refs",
            "scope_digest",
            "bundle_ref",
            "bundle_digest",
            "rule_plan_digest",
            "capability_registry_digest",
            "tool_roster",
            "deadlines",
            "budgets",
        ),
        agent_owned_fields=(
            "tool_intent",
            "outcome_status",
            "risk_level",
            "risk_rationale",
            "confidence",
            "confidence_rationale",
            "analysis_detail",
            "attribution_summary",
        ),
    )
    values = {
        "schema_version": 1,
        "rules": rules,
        "phase_permissions": permissions,
        "ownership": ownership,
    }
    return ReasoningPromptPolicy(
        **values,
        policy_digest=canonical_json_digest(values),
    )


def _json_value(value: object) -> object:
    if isinstance(value, DomainModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


DEFAULT_REASONING_PROMPT_POLICY = _build_default_policy()
