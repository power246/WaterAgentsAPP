from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.evidence_tools import EvidenceAcquisitionCellGate
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.application.reasoning import (
    build_reasoning_evidence_catalog,
    ReasoningGateDecision,
    SealedEvidenceValidationError,
    SealedEvidenceInput,
    seal_evidence_input,
    validate_sealed_evidence_input,
)
from app.domain import (
    EvidenceBundle,
    EvidenceCellRole,
    EvidenceCellStatus,
    ItemReference,
    MemoryMode,
)
from tests.application.evidence_tools.conftest import (
    SUPPORT_PARAMETER,
    TARGET_PARAMETER,
    make_discharge_context,
    make_context,
)
from tests.application.reasoning.test_task_and_skeleton import (
    _async_test,
    _validated_fixture,
)


def _replace_window_statuses(
    handoff,
    *,
    target_status,
    discharge_status,
    move_target_to="none",
):
    window_id = handoff.bundle.window_refs[0]
    cells = []
    for cell in handoff.cells:
        if cell.window_id != window_id:
            cells.append(cell)
            continue
        status = (
            target_status
            if cell.role is EvidenceCellRole.TARGET
            else discharge_status
        )
        update = {"status": status, "reasons": ()}
        if status is not EvidenceCellStatus.SUPPORTED:
            update["direct_fragment_refs"] = ()
            update["allowed_indirect_fragment_refs"] = ()
            update["reasons"] = (
                "provider_timeout"
                if status is EvidenceCellStatus.BLOCKED
                else "no_usable_evidence",
            )
            update["contextual_fragment_refs"] = tuple(
                sorted(
                    {
                        *cell.contextual_fragment_refs,
                        *cell.direct_fragment_refs,
                        *cell.allowed_indirect_fragment_refs,
                    }
                )
            )
        elif cell.role is EvidenceCellRole.TARGET and move_target_to == "indirect":
            update["allowed_indirect_fragment_refs"] = cell.direct_fragment_refs
            update["direct_fragment_refs"] = ()
        elif cell.role is EvidenceCellRole.TARGET and move_target_to == "contextual":
            update["contextual_fragment_refs"] = tuple(
                sorted({*cell.contextual_fragment_refs, *cell.direct_fragment_refs})
            )
            update["direct_fragment_refs"] = ()
            update["status"] = EvidenceCellStatus.UNSUPPORTED
            update["reasons"] = ("contextual_only",)
        cells.append(cell.model_copy(update=update))
    bundle = EvidenceBundle.model_validate(
        handoff.bundle.model_copy(update={"cells": tuple(cells)}).model_dump()
    )
    effective_target = (
        EvidenceCellStatus.UNSUPPORTED
        if move_target_to == "contextual"
        else target_status
    )
    statuses = {effective_target, discharge_status}
    gate_status = (
        EvidenceCellStatus.BLOCKED
        if EvidenceCellStatus.BLOCKED in statuses
        else EvidenceCellStatus.SUPPORTED
        if statuses == {EvidenceCellStatus.SUPPORTED}
        else EvidenceCellStatus.UNSUPPORTED
    )
    reasons = []
    if effective_target is not EvidenceCellStatus.SUPPORTED:
        reason = "contextual_only" if move_target_to == "contextual" else (
            "provider_timeout"
            if effective_target is EvidenceCellStatus.BLOCKED
            else "no_usable_evidence"
        )
        reasons.append(f"target:{reason}")
    if discharge_status is not EvidenceCellStatus.SUPPORTED:
        reason = (
            "provider_timeout"
            if discharge_status is EvidenceCellStatus.BLOCKED
            else "no_usable_evidence"
        )
        reasons.append(f"required_discharge:{reason}")
    gates = tuple(
        EvidenceAcquisitionCellGate(
            parameter=gate.parameter,
            window_id=gate.window_id,
            target_status=effective_target,
            discharge_status=discharge_status,
            status=gate_status,
            reasons=tuple(reasons),
        )
        if gate.window_id == window_id
        else gate
        for gate in handoff.cell_gates
    )
    return replace(
        handoff,
        bundle=bundle,
        cells=tuple(cells),
        cell_gates=gates,
        block_facts=("provider_timeout",)
        if gate_status is EvidenceCellStatus.BLOCKED
        else (),
    )


@_async_test
@pytest.mark.parametrize("include_oxygen", [False, True])
async def test_discharge_target_seals_and_validates_its_evidence(
    include_oxygen: bool,
) -> None:
    validated, handoff, registry = await _validated_fixture(
        context=make_discharge_context(include_oxygen=include_oxygen),
    )
    sealed = seal_evidence_input(validated, handoff, registry)
    fragments = tuple([
        await handoff.read_port.read_fragment(
            validated.task.evidence_scope, reference,
        )
        for reference in sealed.fragment_allowlist
    ])
    catalog = build_reasoning_evidence_catalog(sealed, fragments)

    assert sealed.supporting_parameters == ()
    assert all(gate.decision is ReasoningGateDecision.SUPPORTED for gate in sealed.gates)
    for scope in catalog.cell_scopes:
        assert set(scope.allowed_refs) == set(
            next(gate.allowed_evidence_refs for gate in sealed.gates
                 if gate.cell_id == scope.cell_id)
        )
        assert set(scope.target_direct_refs).isdisjoint(scope.support_direct_refs)
        assert set(scope.target_indirect_refs).isdisjoint(scope.support_indirect_refs)


@_async_test
@pytest.mark.parametrize(
    ("target", "discharge", "expected"),
    (
        (
            EvidenceCellStatus.SUPPORTED,
            EvidenceCellStatus.SUPPORTED,
            ReasoningGateDecision.SUPPORTED,
        ),
        (
            EvidenceCellStatus.UNSUPPORTED,
            EvidenceCellStatus.SUPPORTED,
            ReasoningGateDecision.UNSUPPORTED,
        ),
        (
            EvidenceCellStatus.SUPPORTED,
            EvidenceCellStatus.UNSUPPORTED,
            ReasoningGateDecision.UNSUPPORTED,
        ),
        (
            EvidenceCellStatus.BLOCKED,
            EvidenceCellStatus.SUPPORTED,
            ReasoningGateDecision.BLOCKED,
        ),
        (
            EvidenceCellStatus.SUPPORTED,
            EvidenceCellStatus.BLOCKED,
            ReasoningGateDecision.BLOCKED,
        ),
    ),
)
async def test_target_and_same_window_discharge_form_the_only_final_gate(
    target,
    discharge,
    expected,
):
    validated, handoff, registry = await _validated_fixture(mode="full")
    handoff = _replace_window_statuses(
        handoff,
        target_status=target,
        discharge_status=discharge,
    )
    sealed = seal_evidence_input(validated, handoff, registry)

    assert sealed.gates[0].decision is expected
    if expected is ReasoningGateDecision.SUPPORTED:
        assert sealed.gates[0].allowed_evidence_refs
    elif expected is ReasoningGateDecision.BLOCKED:
        assert sealed.gates[0].blocker_refs


@_async_test
async def test_direct_and_materialized_indirect_support_but_contextual_does_not():
    validated, handoff, registry = await _validated_fixture(mode="full")
    direct = seal_evidence_input(validated, handoff, registry)
    indirect = seal_evidence_input(
        validated,
        _replace_window_statuses(
            handoff,
            target_status=EvidenceCellStatus.SUPPORTED,
            discharge_status=EvidenceCellStatus.SUPPORTED,
            move_target_to="indirect",
        ),
        registry,
    )
    contextual = seal_evidence_input(
        validated,
        _replace_window_statuses(
            handoff,
            target_status=EvidenceCellStatus.SUPPORTED,
            discharge_status=EvidenceCellStatus.SUPPORTED,
            move_target_to="contextual",
        ),
        registry,
    )

    assert direct.gates[0].decision is ReasoningGateDecision.SUPPORTED
    assert indirect.gates[0].decision is ReasoningGateDecision.SUPPORTED
    assert contextual.gates[0].decision is ReasoningGateDecision.UNSUPPORTED
    assert contextual.gates[0].allowed_evidence_refs
    assert set(contextual.gates[0].allowed_evidence_refs).isdisjoint(
        set(
            next(
                cell
                for cell in contextual.cells
                if cell.role is EvidenceCellRole.TARGET
                and cell.window_id == contextual.gates[0].window_id
            ).contextual_fragment_refs
        )
    )


@_async_test
async def test_unmaterialized_rule_and_stationless_source_absence_do_not_unlock():
    rule = create_evidence_rule(
        output_parameter=TARGET_PARAMETER,
        input_parameters=(SUPPORT_PARAMETER,),
        temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
        spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
        maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
        required_collection_attempt=False,
        rationale="A declared rule is not materialized evidence.",
        origin=EvidenceRuleOrigin.QUERY_PARSER,
    )
    context = make_context(evidence_rule_plan=create_evidence_rule_plan((rule,)))
    validated, handoff, registry = await _validated_fixture(
        mode="empty",
        context=context,
    )
    sealed = seal_evidence_input(validated, handoff, registry)
    assert {gate.decision for gate in sealed.gates} == {
        ReasoningGateDecision.UNSUPPORTED
    }

    stationless_validated, stationless_handoff, stationless_registry = (
        await _validated_fixture(with_station=False)
    )
    stationless = seal_evidence_input(
        stationless_validated,
        stationless_handoff,
        stationless_registry,
    )
    assert {gate.decision for gate in stationless.gates} == {
        ReasoningGateDecision.UNSUPPORTED
    }
    assert any(
        fact.name == "get_upstream_results"
        and fact.status.value == "unavailable"
        for fact in stationless.producer_facts
    )

    memory_context = make_context(memory_mode=MemoryMode.MEMORY_AUGMENTED)
    memory_validated, memory_handoff, memory_registry = await _validated_fixture(
        mode="full",
        context=memory_context,
    )
    memory_sealed = seal_evidence_input(
        memory_validated,
        memory_handoff,
        memory_registry,
    )
    transcript_fact = next(
        fact
        for fact in memory_sealed.producer_facts
        if fact.name == "search_transcript_evidence"
    )
    assert transcript_fact.authorized_for_scope
    assert transcript_fact.status.value == "unavailable"


@_async_test
async def test_sealed_snapshot_is_canonical_across_input_order_and_has_complete_facts():
    validated, handoff, registry = await _validated_fixture(mode="full")
    first = seal_evidence_input(validated, handoff, registry)
    reversed_bundle = EvidenceBundle.model_validate(
        handoff.bundle.model_copy(
            update={
                "cells": tuple(reversed(handoff.bundle.cells)),
                "fragment_refs": tuple(reversed(handoff.bundle.fragment_refs)),
                "tool_result_refs": tuple(reversed(handoff.bundle.tool_result_refs)),
            }
        ).model_dump()
    )
    reordered = replace(
        handoff,
        bundle=reversed_bundle,
        cells=reversed_bundle.cells,
        fragment_allowlist=reversed_bundle.fragment_refs,
        tool_result_refs=reversed_bundle.tool_result_refs,
        collection_fact_refs=tuple(reversed(handoff.collection_fact_refs)),
        producer_facts=tuple(reversed(handoff.producer_facts)),
        cell_gates=tuple(reversed(handoff.cell_gates)),
    )
    second = seal_evidence_input(validated, reordered, registry)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.sealed_digest == second.sealed_digest
    assert first.rule_plan_digest == validated.context.evidence_rule_plan.plan_digest
    assert first.capability_registry_digest == registry.manifest_digest
    assert first.planning_summary.fragment_count == len(first.fragment_allowlist)
    assert first.planning_summary.collection_fact_count == len(
        first.collection_fact_refs
    )
    assert first.planning_summary.tool_result_count == len(first.tool_result_refs)
    assert first.budget_usage.collection_used > 0
    assert {fact.name for fact in first.producer_facts} == {
        "get_upstream_results",
        "search_transcript_evidence",
    }


@_async_test
async def test_sealed_input_rejects_tampered_identity_ref_digest_and_count():
    validated, handoff, registry = await _validated_fixture(mode="full")
    sealed = seal_evidence_input(validated, handoff, registry)

    forged_handoff = replace(
        handoff,
        bundle_ref=ItemReference(item_id="bundle-tampered"),
    )
    with pytest.raises(
        SealedEvidenceValidationError,
        match="bundle_identity_mismatch",
    ):
        seal_evidence_input(validated, forged_handoff, registry)

    mutations = (
        ("run_id", UUID("00000000-0000-0000-0000-000000000099")),
        ("bundle_ref", ItemReference(item_id="bundle-tampered")),
        ("rule_plan_digest", "sha256:tampered"),
        (
            "planning_summary",
            sealed.planning_summary.model_copy(update={"fragment_count": 999}),
        ),
        ("sealed_digest", "sha256:tampered"),
    )
    for field, value in mutations:
        tampered = sealed.model_copy(update={field: value})
        with pytest.raises(ValidationError):
            validate_sealed_evidence_input(tampered)

    raw = sealed.model_dump(mode="json")
    raw["gates"][0]["allowed_evidence_refs"].append("evidence:outside")
    with pytest.raises(ValidationError):
        SealedEvidenceInput.model_validate(raw)
