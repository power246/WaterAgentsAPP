import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_models import DependencyStatus
from app.application.evidence_tools import create_evidence_tool_registry
from app.application.reasoning.model_models import (
    WaterbodyJsonSchema,
    WaterbodyModelPhase,
)
from app.application.reasoning.prompt_builder import (
    EvidencePlanningPromptInput,
    FixedCellPromptSummary,
    OverallSynthesisPromptInput,
    PromptBudgetRemaining,
    PromptBuilder,
    PromptValidationIssue,
    ReasoningBatchPromptInput,
    SchemaRepairPromptInput,
    ValidatedCellPromptSummary,
    WaterbodyStationKind,
    audit_prompt_contract,
    project_authorized_tool_roster,
)
from app.application.reasoning.prompt_policy import (
    DEFAULT_REASONING_PROMPT_POLICY,
    PromptPolicyRule,
    audit_prompt_policy,
)
from app.application.reasoning.result_models import AgentReasoningCellScope
from app.application.reasoning.sufficiency import (
    ReasoningCellGate,
    ReasoningGateDecision,
)
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    DependencyPromptEntry,
    DependencyPromptWindow,
    WaterbodyDependencyPromptBlock,
    WorkingContextFact,
    WorkingContextFactKind,
    build_working_context_projection,
)
from app.domain import (
    AggregationMethod,
    EvidenceCellStatus,
    InferenceMode,
    ItemReference,
    ModelSelection,
    OutcomeStatus,
    ParameterKey,
    ResultSource,
    RiskLevel,
)
from app.infrastructure.config import load_parameter_capability_registry

OPERATION_IDS = {
    WaterbodyModelPhase.EVIDENCE_PLANNING: UUID(
        "30000000-0000-4000-8000-000000000001"
    ),
    WaterbodyModelPhase.REASONING_BATCH: UUID(
        "30000000-0000-4000-8000-000000000002"
    ),
    WaterbodyModelPhase.OVERALL_SYNTHESIS: UUID(
        "30000000-0000-4000-8000-000000000003"
    ),
    WaterbodyModelPhase.SCHEMA_REPAIR: UUID(
        "30000000-0000-4000-8000-000000000004"
    ),
}
MODEL = ModelSelection(provider="openai", model="waterbody-test")
PARAMETER = ParameterKey(code_system="usgs", code="00010")
DEADLINE = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
RUN_ID = UUID("10000000-0000-4000-8000-000000000001")


def response_schema(name: str, *, reverse: bool = False) -> WaterbodyJsonSchema:
    properties = {
        "decision_type": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object"}},
    }
    if reverse:
        properties = dict(reversed(tuple(properties.items())))
    return WaterbodyJsonSchema(
        name=name,
        schema_text=json.dumps(
            {
                "type": "object",
                "properties": properties,
                "required": ["decision_type", "items"],
                "additionalProperties": False,
            }
        ),
    )


def working_context(*, outside_summary: bool = False, dependency_context=None):
    evidence_ref = "outside-evidence" if outside_summary else "evidence-1"
    allowlist = (
        "cell-1",
        "evidence-1",
        "source-item-1",
        "blocker-1",
    )
    if outside_summary:
        allowlist = (*allowlist, evidence_ref)
    return build_working_context_projection(
        run_id=RUN_ID,
        session_id=UUID("20000000-0000-4000-8000-000000000001"),
        target_comid="12345678",
        scope_digest="sha256:scope",
        bundle_ref=ItemReference(item_id="bundle-item-1"),
        bundle_digest="bundle:sha256:bundle",
        rule_plan_digest="rule:sha256:rules",
        capability_registry_digest="sha256:capabilities",
        reference_allowlist=allowlist,
        recent_messages=(),
        facts=(
            WorkingContextFact(
                fact_id="coverage-1",
                kind=WorkingContextFactKind.COVERAGE,
                summary="Coverage is partial because one source is unavailable.",
                refs=("blocker-1",),
            ),
        ),
        evidence_summaries=(
            BoundedEvidenceSummary(
                evidence_ref=evidence_ref,
                summary="Temperature evidence summary with valid quality.",
                source_record_item_refs=("source-item-1",),
                series_refs=("series-1",),
                truncated=False,
            ),
        ),
        dependency_context=dependency_context,
    )


def supported_gate() -> ReasoningCellGate:
    return ReasoningCellGate(
        cell_id="cell-1",
        parameter=PARAMETER,
        window_id="window-1",
        target_status=EvidenceCellStatus.SUPPORTED,
        discharge_status=EvidenceCellStatus.SUPPORTED,
        decision=ReasoningGateDecision.SUPPORTED,
        allowed_evidence_refs=("evidence-1",),
        contextual_refs=(),
        collection_fact_refs=(),
        blocker_refs=(),
        reasons=(),
    )


def batch_cell() -> AgentReasoningCellScope:
    return AgentReasoningCellScope(
        cell_id="cell-1",
        parameter=PARAMETER,
        window_id="window-1",
        start_time=datetime(2026, 10, 2, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 2, 6, tzinfo=timezone.utc),
        mode="gap_filling",
        allowed_evidence_refs=("evidence-1",),
        allowed_result_sources=(ResultSource.OBSERVED, ResultSource.INFERRED),
        allowed_unit_ids=("degC",),
        allowed_aggregation_methods=(AggregationMethod.MEAN,),
    )


def common(phase: WaterbodyModelPhase, schema_name: str) -> dict[str, object]:
    return {
        "operation_id": OPERATION_IDS[phase],
        "model": MODEL,
        "deadline": DEADLINE,
        "context": working_context(),
        "response_schema": response_schema(schema_name),
    }


def build_all_requests():
    registry = create_evidence_tool_registry(load_parameter_capability_registry())
    selected = tuple(
        descriptor
        for descriptor in registry.descriptors
        if descriptor.name
        in {
            "collect_weather_evidence",
            "convert_evidence_unit",
            "aggregate_evidence",
            "inspect_evidence_records",
        }
    )
    builder = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY)
    planning = builder.build_evidence_planning(
        EvidencePlanningPromptInput(
            **common(WaterbodyModelPhase.EVIDENCE_PLANNING, "planning_decision"),
            station_kind=WaterbodyStationKind.STATION_BACKED,
            gates=(supported_gate(),),
            budget=PromptBudgetRemaining(
                collection_calls=3,
                transform_calls=4,
                planning_turns=2,
                calls_this_turn=2,
            ),
            authorized_tools=project_authorized_tool_roster(selected),
        )
    )
    batch = builder.build_reasoning_batch(
        ReasoningBatchPromptInput(
            **common(WaterbodyModelPhase.REASONING_BATCH, "reasoning_cell_batch"),
            batch_id="batch-1",
            cells=(batch_cell(),),
        )
    )
    synthesis = builder.build_overall_synthesis(
        OverallSynthesisPromptInput(
            **common(WaterbodyModelPhase.OVERALL_SYNTHESIS, "overall_synthesis"),
            validated_cells=(
                ValidatedCellPromptSummary(
                    cell_id="cell-1",
                    window_id="window-1",
                    outcome_status=OutcomeStatus.QUANTIFIED,
                    risk_level=RiskLevel.LOW,
                    risk_rationale="The evidence indicates a limited risk signal.",
                    confidence=0.8,
                    confidence_rationale="Direct evidence has usable coverage.",
                    evidence_refs=("evidence-1",),
                ),
            ),
            fixed_cells=(
                FixedCellPromptSummary(
                    cell_id="cell-fixed",
                    window_id="window-2",
                    reasons=("insufficient_evidence",),
                    blocker_refs=("blocker-1",),
                ),
            ),
        )
    )
    repair = builder.build_schema_repair(
        SchemaRepairPromptInput(
            **common(WaterbodyModelPhase.SCHEMA_REPAIR, "reasoning_cell_batch"),
            repair_of_operation_id=OPERATION_IDS[
                WaterbodyModelPhase.REASONING_BATCH
            ],
            original_phase=WaterbodyModelPhase.REASONING_BATCH,
            batch_id="batch-1",
            original_visible_response='{"decision_type":"batch","items":[]}',
            issues=(
                PromptValidationIssue(code="missing_cell", path="items[0].cell_id"),
            ),
        )
    )
    return planning, batch, synthesis, repair


def test_policy_source_is_typed_immutable_complete_and_conflict_free() -> None:
    policy = DEFAULT_REASONING_PROMPT_POLICY
    report = audit_prompt_policy(policy)

    assert report.failures == ()
    assert report.rule_count == len(policy.rules)
    assert policy.policy_digest.startswith("sha256:")
    assert {permission.phase for permission in policy.phase_permissions} == set(
        WaterbodyModelPhase
    )
    assert set(policy.ownership.server_owned_fields).isdisjoint(
        policy.ownership.agent_owned_fields
    )
    with pytest.raises(ValidationError):
        policy.rules += (
            PromptPolicyRule(
                rule_id="extra_rule",
                phases=tuple(WaterbodyModelPhase),
                instruction="An immutable extra rule.",
            ),
        )


def test_policy_audit_rejects_semantically_incomplete_required_rule() -> None:
    policy = DEFAULT_REASONING_PROMPT_POLICY
    weakened_rules = tuple(
        rule.model_copy(update={"instruction": "Risk uses evidence."})
        if rule.rule_id == "risk_levels"
        else rule
        for rule in policy.rules
    )
    weakened = policy.model_copy(update={"rules": weakened_rules})

    report = audit_prompt_policy(weakened)

    assert "rule_semantics_incomplete" in {
        failure.code for failure in report.failures
    }


def test_four_phase_prompts_share_policy_and_enforce_permissions() -> None:
    requests = build_all_requests()
    planning, batch, synthesis, repair = requests

    assert {request.phase for request in requests} == set(WaterbodyModelPhase)
    assert {request.prompt_policy_digest for request in requests} == {
        DEFAULT_REASONING_PROMPT_POLICY.policy_digest
    }
    assert tuple(tool.name for tool in planning.tool_descriptors) == (
        "collect_weather_evidence",
        "convert_evidence_unit",
        "aggregate_evidence",
        "inspect_evidence_records",
    )
    assert batch.tool_descriptors == ()
    assert synthesis.tool_descriptors == ()
    assert repair.tool_descriptors == ()
    assert batch.batch_id == "batch-1"
    assert repair.batch_id == "batch-1"

    base_rule_ids = tuple(
        rule.rule_id
        for rule in DEFAULT_REASONING_PROMPT_POLICY.rules
        if rule.applies_to_all_phases
    )
    for request in requests:
        assert all(f"[{rule_id}]" in request.system_prompt for rule_id in base_rule_ids)


def test_planning_schema_is_exactly_projected_from_authorized_registry_roster() -> None:
    planning = build_all_requests()[0]
    rendered = "\n".join(
        (planning.system_prompt, *(message.text for message in planning.messages))
    )

    assert '"station_kind":"station_backed"' in rendered
    assert '"bundle_digest":"bundle:sha256:bundle"' in rendered
    assert '"planning_turns":2' in rendered
    assert '"decision":"supported"' in rendered
    for tool in planning.tool_descriptors:
        schema_body = json.loads(tool.input_schema.schema_text)
        assert schema_body["additionalProperties"] is False
        properties = schema_body["properties"]
        assert set(properties) == {"window_ids", "parameter_keys", "options"}
        assert not {
            "provider",
            "comid",
            "station_id",
            "url",
            "credential",
            "session_id",
        } & set(properties)


def test_batch_synthesis_and_repair_inputs_are_phase_bounded() -> None:
    _, batch, synthesis, repair = build_all_requests()
    batch_text = batch.messages[-1].text
    synthesis_text = synthesis.messages[-1].text
    repair_text = repair.messages[-1].text

    assert '"cell_id":"cell-1"' in batch_text
    assert '"evidence_ref":"evidence-1"' in batch_text
    assert "cell-fixed" not in batch_text
    assert '"validated_cells"' in synthesis_text
    assert '"fixed_cells"' in synthesis_text
    assert '"coverage_facts"' in synthesis_text
    assert "source_records" not in synthesis_text
    assert '"original_visible_response"' in repair_text
    assert '"code":"missing_cell"' in repair_text
    assert "evidence_summaries" not in repair_text
    assert "validated_cells" not in repair_text
    assert "switch model" not in repair.system_prompt.lower()


def test_synthesis_keeps_server_bounded_contextual_summary_for_attribution() -> None:
    payload = common(WaterbodyModelPhase.OVERALL_SYNTHESIS, "overall_synthesis")
    payload["context"] = working_context(outside_summary=True)
    request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_overall_synthesis(
        OverallSynthesisPromptInput(
            **payload,
            validated_cells=(
                ValidatedCellPromptSummary(
                    cell_id="cell-1",
                    window_id="window-1",
                    outcome_status=OutcomeStatus.QUANTIFIED,
                    risk_level=RiskLevel.LOW,
                    risk_rationale="The evidence indicates a limited risk signal.",
                    confidence=0.8,
                    confidence_rationale="Direct evidence has usable coverage.",
                    evidence_refs=("evidence-1",),
                ),
            ),
            fixed_cells=(),
        )
    )

    body = json.loads(request.messages[-1].text)
    assert body["bounded_reference_summaries"][0]["evidence_ref"] == (
        "outside-evidence"
    )


def test_dependency_prompt_locks_source_roles_numeric_boundary_and_windows() -> None:
    dependency_context = WaterbodyDependencyPromptBlock(
        manifest_digest="sha256:dependency-manifest",
        current_waterbody_comid="12345678",
        mode=InferenceMode.MIXED,
        windows=(
            DependencyPromptWindow(
                window_id="window-gap",
                mode=InferenceMode.GAP_FILLING,
            ),
            DependencyPromptWindow(
                window_id="window-prediction",
                mode=InferenceMode.PREDICTION,
            ),
        ),
        dependencies=(
            DependencyPromptEntry(
                upstream_comid="100",
                dependency_status=DependencyStatus.USABLE,
                result_ref=SessionResultReference(
                    run_id=RUN_ID,
                    item_id=UUID("40000000-0000-0000-0000-000000000100"),
                ),
            ),
            DependencyPromptEntry(
                upstream_comid="200",
                dependency_status=DependencyStatus.INVALID,
            ),
        ),
    )
    payload = common(WaterbodyModelPhase.EVIDENCE_PLANNING, "planning_decision")
    payload["context"] = working_context(dependency_context=dependency_context)
    request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_evidence_planning(
        EvidencePlanningPromptInput(
            **payload,
            station_kind=WaterbodyStationKind.STATION_BACKED,
            gates=(supported_gate(),),
            budget=PromptBudgetRemaining(
                collection_calls=1,
                transform_calls=1,
                planning_turns=1,
                calls_this_turn=1,
            ),
            authorized_tools=(),
        )
    )

    body = json.loads(request.messages[-1].text)
    block = body["dependency_context"]
    assert block["mode"] == "mixed"
    assert block["windows"] == [
        {"window_id": "window-gap", "mode": "gap_filling"},
        {"window_id": "window-prediction", "mode": "prediction"},
    ]
    assert block["local_same_parameter_role"] == "direct_observation"
    assert block["local_supporting_role"] == "supporting_indirect_evidence"
    assert block["upstream_role"] == "cross_waterbody_inferred_evidence"
    assert block["quantified_value_policy"] == "registered_deterministic_tool_only"
    assert block["dependencies"][0]["result_ref"] == {
        "run_id": str(RUN_ID),
        "item_id": "40000000-0000-0000-0000-000000000100",
    }
    invalid = block["dependencies"][1]
    assert invalid == {
        "upstream_comid": "200",
        "dependency_status": "invalid",
        "source_role": "cross_waterbody_inferred_evidence",
    }
    rendered = request.system_prompt.lower()
    for phrase in (
        "current-waterbody",
        "direct observation",
        "upstream-waterbody",
        "inferred evidence",
        "local discharge",
        "directional",
        "registered deterministic",
        "never invent",
    ):
        assert phrase in rendered


def test_reasoning_batch_rejects_scope_external_evidence_before_rendering() -> None:
    payload = common(WaterbodyModelPhase.REASONING_BATCH, "reasoning_cell_batch")
    payload["context"] = working_context(outside_summary=True)

    with pytest.raises(ValidationError, match="batch evidence scope"):
        ReasoningBatchPromptInput(
            **payload,
            batch_id="batch-1",
            cells=(batch_cell(),),
        )


def test_schema_key_order_does_not_change_request_or_canonical_digests() -> None:
    builder = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY)
    first_input = ReasoningBatchPromptInput(
        **common(WaterbodyModelPhase.REASONING_BATCH, "reasoning_cell_batch"),
        batch_id="batch-1",
        cells=(batch_cell(),),
    )
    second_input = first_input.model_copy(
        update={"response_schema": response_schema("reasoning_cell_batch", reverse=True)}
    )

    first = builder.build_reasoning_batch(first_input)
    second = builder.build_reasoning_batch(second_input)

    assert first == second


def test_prompt_audit_records_phase_rosters_schema_digests_and_failure_sample() -> None:
    requests = build_all_requests()
    report = audit_prompt_contract(DEFAULT_REASONING_PROMPT_POLICY, requests)

    assert report.failures == ()
    assert report.rule_count == len(DEFAULT_REASONING_PROMPT_POLICY.rules)
    assert tuple(item.phase for item in report.phases) == tuple(WaterbodyModelPhase)
    assert all(item.schema_digest.startswith("sha256:") for item in report.phases)
    assert all(item.prompt_digest.startswith("sha256:") for item in report.phases)
    assert report.phases[0].tool_names
    assert all(not item.tool_names for item in report.phases[1:])

    bad_request = requests[1].model_copy(update={"prompt_policy_digest": "sha256:bad"})
    failed = audit_prompt_contract(
        DEFAULT_REASONING_PROMPT_POLICY,
        (requests[0], bad_request, requests[2], requests[3]),
    )
    assert "prompt_policy_digest_mismatch" in {
        failure.code for failure in failed.failures
    }


def test_policy_requires_five_risks_rationales_series_units_gates_and_safety() -> None:
    instructions = {
        rule.rule_id: rule.instruction
        for rule in DEFAULT_REASONING_PROMPT_POLICY.rules
    }

    assert all(
        level in instructions["risk_levels"]
        for level in ("none", "low", "medium", "high", "indeterminate")
    )
    assert "evidence" in instructions["risk_rationale"]
    assert "interpretation" in instructions["risk_rationale"]
    assert "confidence" in instructions["confidence_semantics"]
    assert "series" in instructions["series_review"]
    assert "observed" in instructions["source_eligibility"]
    assert "deterministic" in instructions["unit_and_aggregation"]
    assert "gate" in instructions["evidence_eligibility"]
    assert "credentials" in instructions["secret_safety"]
    assert "concealed" in instructions["visible_reasoning_only"]
    assert "none" in instructions["absence_is_not_none"]
    assert not any(
        marker in "\n".join(instructions.values()).lower()
        for marker in ("0-100 score", "take the maximum risk", "average risk levels")
    )


def test_rendered_prompts_contain_no_secret_archive_raw_provider_or_outside_scope() -> None:
    rendered = "\n".join(
        text
        for request in build_all_requests()
        for text in (
            request.system_prompt,
            *(message.text for message in request.messages),
            *(tool.input_schema.schema_text for tool in request.tool_descriptors),
        )
    ).lower()

    for forbidden in (
        "private-api-key-value",
        "authorization: bearer",
        "raw_provider_envelope",
        "back" + "end_bk",
        "front" + "end_bk",
        "outside-comid",
        "outside-station",
        "outside-evidence",
        "encrypted_reasoning",
    ):
        assert forbidden not in rendered
