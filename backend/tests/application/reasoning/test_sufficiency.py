import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from app.application.canonical_json import canonical_json_digest
from app.application.evidence_tools import (
    EvidenceAcquisitionCellGate,
    with_intermediate_parameters,
)
from app.application.evidence_tools.acquisition import (
    _bundle_binding_digest,
    _cell_gates,
)
from app.application.evidence_tools.bundle_builder import EvidenceBundleBuilder
from app.application.evidence_tools.numeric_dependency import (
    NumericDependencyExecutor,
    NumericDependencyPlanner,
    prepare_numeric_scope,
)
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
)
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.application.reasoning import (
    DraftValidationError,
    DraftValidator,
    ReasoningGateDecision,
    SealedEvidenceInput,
    SealedEvidenceValidationError,
    build_reasoning_evidence_catalog,
    build_result_skeleton,
    seal_evidence_input,
    validate_sealed_evidence_input,
)
from app.application.reasoning.planning_loop import (
    build_reasoning_evidence_summaries,
)
from app.application.reasoning.working_context import (
    build_working_context_projection,
)
from app.domain import (
    AggregatePayload,
    EvidenceBundle,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceFragment,
    ItemReference,
    MemoryMode,
    NumericDerivedPayload,
    ParameterKey,
    TemporalEstimatePayload,
)
from pydantic import ValidationError
from tests.application.evidence_tools.conftest import (
    SUPPORT_PARAMETER,
    TARGET_PARAMETER,
    WEATHER_PARAMETER,
    make_context,
    make_discharge_context,
)
from tests.application.evidence_tools.test_acquisition_workflow import (
    _PersistedPolicyStore,
    _runtime,
    _sparse_continuous_port,
)
from tests.application.reasoning.test_batch_planner import _plan
from tests.application.reasoning.test_task_and_skeleton import (
    NOW,
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
    gate_status = effective_target
    reasons = []
    if effective_target is not EvidenceCellStatus.SUPPORTED:
        reason = "contextual_only" if move_target_to == "contextual" else (
            "provider_timeout"
            if effective_target is EvidenceCellStatus.BLOCKED
            else "no_usable_evidence"
        )
        reasons.append(f"target:{reason}")
    if (gate_status is not EvidenceCellStatus.SUPPORTED
            and discharge_status is not EvidenceCellStatus.SUPPORTED):
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
async def test_sparse_measured_windows_and_temporal_estimates_build_m6_catalog() -> None:
    context = make_context(
        decision_instant=datetime(2026, 10, 2, tzinfo=UTC)
    )
    validated, _initial_handoff, registry = await _validated_fixture(context=context)
    scope = validated.task.evidence_scope
    store = _PersistedPolicyStore()
    handoff = await _runtime(
        store, water_port=_sparse_continuous_port(scope)
    ).start(scope)
    sealed = seal_evidence_input(validated, handoff, registry)
    fragments = tuple([
        await handoff.read_port.read_fragment(scope, reference)
        for reference in sealed.fragment_allowlist
    ])
    catalog = build_reasoning_evidence_catalog(sealed, fragments)

    assert all(gate.decision is ReasoningGateDecision.SUPPORTED for gate in sealed.gates)
    planned_temporal_refs = {
        fragment.payload.plan_item_ref.item_id
        for fragment in fragments
        if isinstance(fragment.payload, TemporalEstimatePayload)
        and fragment.payload.plan_item_ref is not None
    }
    assert planned_temporal_refs
    assert planned_temporal_refs <= set(sealed.numeric_plan_item_refs)
    for fragment in fragments:
        if not isinstance(fragment.payload, TemporalEstimatePayload):
            continue
        fact = next(
            item for item in catalog.facts
            if item.evidence_ref == fragment.evidence_id
        )
        assert fact.temporal_method == fragment.payload.method
        assert fact.anchor_distance_seconds == fragment.payload.anchor_distance_seconds
        assert fact.dependency_evidence_refs == tuple(
            reference.evidence_id
            for reference in fragment.payload.input_evidence_refs
        )


@_async_test
async def test_planned_temporal_catalog_requires_sealed_plan_reference() -> None:
    context = make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    validated, _initial_handoff, registry = await _validated_fixture(context=context)
    scope = validated.task.evidence_scope
    store = _PersistedPolicyStore()
    handoff = await _runtime(
        store, water_port=_sparse_continuous_port(scope)
    ).start(scope)
    sealed = seal_evidence_input(validated, handoff, registry)
    fragments = tuple([
        await handoff.read_port.read_fragment(scope, reference)
        for reference in sealed.fragment_allowlist
    ])
    estimate = next(
        fragment
        for fragment in fragments
        if isinstance(fragment.payload, TemporalEstimatePayload)
    )
    plan_ref = ItemReference(item_id="planned-temporal-item")
    payload = estimate.payload.model_copy(update={
        "plan_item_ref": plan_ref,
        "task_id": "numeric:sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "method_registry_digest": "sha256:" + "c" * 64,
    })
    planned_estimate = EvidenceFragment.model_validate(
        estimate.model_copy(update={"payload": payload}).model_dump()
    )
    planned_fragments = tuple(
        planned_estimate if fragment.evidence_id == estimate.evidence_id else fragment
        for fragment in fragments
    )
    with pytest.raises(ValueError, match="temporal derivation plan"):
        build_reasoning_evidence_catalog(sealed, planned_fragments)

    values = sealed.model_dump(mode="python", exclude={"sealed_digest"})
    values["numeric_plan_item_refs"] = tuple(sorted({
        *sealed.numeric_plan_item_refs,
        plan_ref.item_id,
    }))
    planned_sealed = SealedEvidenceInput(
        **values, sealed_digest=canonical_json_digest(values)
    )
    catalog = build_reasoning_evidence_catalog(planned_sealed, planned_fragments)
    assert any(
        fact.evidence_ref == estimate.evidence_id for fact in catalog.facts
    )


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
            ReasoningGateDecision.SUPPORTED,
        ),
        (
            EvidenceCellStatus.BLOCKED,
            EvidenceCellStatus.SUPPORTED,
            ReasoningGateDecision.BLOCKED,
        ),
        (
            EvidenceCellStatus.SUPPORTED,
            EvidenceCellStatus.BLOCKED,
            ReasoningGateDecision.SUPPORTED,
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
async def test_flow_context_for_target_remains_usable_only_as_required_support():
    validated, handoff, registry = await _validated_fixture(mode="full")
    handoff = _replace_window_statuses(
        handoff, target_status=EvidenceCellStatus.SUPPORTED,
        discharge_status=EvidenceCellStatus.SUPPORTED, move_target_to="contextual",
    )
    window = handoff.bundle.window_refs[0]
    support = next(cell for cell in handoff.cells if cell.window_id == window
                   and cell.role is EvidenceCellRole.REQUIRED_SUPPORT)
    target = next(cell for cell in handoff.cells if cell.window_id == window
                  and cell.role is EvidenceCellRole.TARGET)
    flow_refs = support.direct_fragment_refs
    assert flow_refs
    cells = tuple(cell.model_copy(update={
        "contextual_fragment_refs": tuple(sorted(set(target.contextual_fragment_refs + flow_refs))),
    }) if cell is target else cell for cell in handoff.cells)
    bundle = EvidenceBundle.model_validate(handoff.bundle.model_copy(update={"cells": cells}).model_dump())
    handoff = replace(handoff, bundle=bundle, cells=cells)

    sealed = seal_evidence_input(validated, handoff, registry)

    gate = sealed.gates[0]
    assert gate.decision is ReasoningGateDecision.UNSUPPORTED
    assert gate.target_status is EvidenceCellStatus.UNSUPPORTED
    assert gate.discharge_status is EvidenceCellStatus.SUPPORTED
    assert set(flow_refs) <= set(gate.allowed_evidence_refs)
    assert set(gate.allowed_evidence_refs).isdisjoint(gate.contextual_refs)
    assert next(cell for cell in sealed.cells if cell.parameter == target.parameter
                and cell.window_id == window).direct_fragment_refs == ()


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
@pytest.mark.parametrize("move_target_to", ("indirect", "none"))
async def test_missing_discharge_does_not_fix_supported_target(
    move_target_to,
):
    validated, handoff, registry = await _validated_fixture(mode="full")
    handoff = _replace_window_statuses(
        handoff,
        target_status=EvidenceCellStatus.SUPPORTED,
        discharge_status=EvidenceCellStatus.UNSUPPORTED,
        move_target_to=move_target_to,
    )
    sealed = seal_evidence_input(validated, handoff, registry)
    skeleton = build_result_skeleton(
        validated, sealed, registry, result_id="missing-discharge", generated_at=NOW,
    )

    gate = sealed.gates[0]
    assert gate.decision is ReasoningGateDecision.SUPPORTED
    assert gate.reasons == ()
    assert gate.allowed_evidence_refs
    assert skeleton.fixed_cells == ()
    assert skeleton.supported_scopes[0].allowed_evidence_refs == gate.allowed_evidence_refs


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
async def test_intermediate_cells_are_sealed_but_not_requested_result_gates():
    validated, handoff, registry = await _validated_fixture(mode="full")
    original_validated = validated
    scope = with_intermediate_parameters(
        validated.task.evidence_scope, (WEATHER_PARAMETER,)
    )
    task = replace(validated.task, evidence_scope=scope)
    validated = replace(validated, task=task)
    method_cells = tuple(
        EvidenceCell(
            parameter=WEATHER_PARAMETER,
            window_id=window_id,
            role=EvidenceCellRole.METHOD_INPUT,
            status=EvidenceCellStatus.UNSUPPORTED,
            reasons=("no_usable_evidence",),
        )
        for window_id in handoff.bundle.window_refs
    )
    bundle = EvidenceBundle.model_validate(
        handoff.bundle.model_copy(
            update={
                "intermediate_parameters": (WEATHER_PARAMETER,),
                "cells": (*handoff.bundle.cells, *method_cells),
            }
        ).model_dump()
    )
    handoff = replace(
        handoff,
        bundle=bundle,
        cells=bundle.cells,
        bundle_binding_digest=_bundle_binding_digest(handoff.bundle_ref, bundle),
    )

    sealed = seal_evidence_input(validated, handoff, registry)

    assert sealed.intermediate_parameters == (WEATHER_PARAMETER,)
    assert sealed.cells[-len(method_cells):] == method_cells
    assert len(sealed.gates) == len(handoff.bundle.window_refs)
    skeleton = build_result_skeleton(
        validated, sealed, registry, result_id="intermediate-only", generated_at=NOW
    )
    assert skeleton.requested_parameters == (TARGET_PARAMETER,)
    assert all(gate.parameter == TARGET_PARAMETER for gate in sealed.gates)
    assert all(
        not cell_id.startswith("cell:usgs:00045:")
        for cell_id in skeleton.cell_order
    )

    with pytest.raises(
        SealedEvidenceValidationError, match="bundle_intermediate_mismatch"
    ):
        seal_evidence_input(original_validated, handoff, registry)


@_async_test
async def test_saved_numeric_chain_reaches_sealed_target_and_draft_validation():
    context = make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
    validated, baseline, capabilities = await _validated_fixture(
        mode="full", context=context
    )
    store = baseline.read_port
    flow = tuple(
        (reference, fragment)
        for reference, fragment in baseline.validated_inputs.fragments
        if fragment.parameter_refs == (SUPPORT_PARAMETER,)
        and isinstance(fragment.payload, AggregatePayload)
    )
    assert len(flow) == len(validated.task.evidence_scope.time_windows)
    flow_unit = flow[0][1].payload.unit
    target_unit = capabilities.get(TARGET_PARAMETER).canonical_unit
    a, b, c, d = tuple(
        ParameterKey(code_system="usgs", code=code)
        for code in ("90011", "90012", "90013", "90014")
    )

    def method(name, output, output_unit, inputs, calculation):
        return NumericMethod(
            method_id=f"test_m6_chain_{name}",
            version=1,
            output_parameter=output,
            output_unit=output_unit,
            inputs=inputs,
            calculate=calculation,
            confidence_cap=0.6,
        )

    flow_input = NumericMethodInput(parameter=SUPPORT_PARAMETER, unit=flow_unit)
    input_for = lambda parameter: NumericMethodInput(parameter=parameter, unit="unit")
    numeric_registry = NumericMethodRegistry((
        method("a", a, "unit", (flow_input,),
               lambda values, _ranges, _target: values[0]),
        method("b", b, "unit", (flow_input,),
               lambda values, _ranges, _target: values[0] * 2),
        method("c", c, "unit", (input_for(a), input_for(b)),
               lambda values, _ranges, _target: sum(values, Decimal(0))),
        method("d", d, "unit", (flow_input,),
               lambda values, _ranges, _target: values[0] * 3),
        method("t", TARGET_PARAMETER, target_unit,
               (input_for(c), input_for(d)),
               lambda values, _ranges, _target: sum(values, Decimal(0))),
    ))
    scope = prepare_numeric_scope(validated.task.evidence_scope, numeric_registry)
    validated = replace(
        validated, task=replace(validated.task, evidence_scope=scope)
    )
    source_refs = tuple(reference for reference, _ in flow)
    dependency_plan = await NumericDependencyPlanner(
        registry=numeric_registry, reader=store
    ).plan(scope, source_refs)
    assert len(dependency_plan.tasks) == len(scope.time_windows) * 5
    outcome = await NumericDependencyExecutor(
        registry=numeric_registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(3),
    ).run(scope, dependency_plan)
    assert not outcome.blocked_task_ids and not outcome.failed_task_ids
    assert len(outcome.fragment_refs) == len(dependency_plan.tasks)
    saved = await store.read_fragments(scope, outcome.fragment_refs)
    saved_by_key = {
        (fragment.parameter_refs[0], fragment.time_range): (reference, fragment)
        for reference, fragment in zip(outcome.fragment_refs, saved, strict=True)
    }
    for source_ref, source_fragment in flow:
        window = source_fragment.time_range
        a_ref, _ = saved_by_key[(a, window)]
        b_ref, _ = saved_by_key[(b, window)]
        c_ref, c_fragment = saved_by_key[(c, window)]
        d_ref, _ = saved_by_key[(d, window)]
        _, target_fragment = saved_by_key[(TARGET_PARAMETER, window)]
        assert c_fragment.payload.input_evidence_refs == (a_ref, b_ref)
        assert target_fragment.payload.input_evidence_refs == (c_ref, d_ref)
        assert Decimal(target_fragment.payload.value) == (
            Decimal(source_fragment.payload.value) * 6
        )
        assert saved_by_key[(a, window)][1].payload.input_evidence_refs == (
            source_ref,
        )

    built = await EvidenceBundleBuilder(
        reader=store, writer=store, numeric_registry=numeric_registry
    ).build_and_write(
        scope,
        fragment_refs=(*source_refs, *outcome.fragment_refs),
        tool_result_refs=(),
        created_at=NOW,
    )
    bundle = built.bundle
    handoff = replace(
        baseline,
        bundle_ref=built.reference,
        bundle=bundle,
        validated_inputs=built.validated_inputs,
        bundle_binding_digest=_bundle_binding_digest(built.reference, bundle),
        cells=bundle.cells,
        collection_fact_refs=(),
        fragment_allowlist=bundle.fragment_refs,
        source_record_allowlist=bundle.source_record_item_refs,
        tool_result_refs=bundle.tool_result_refs,
        cell_gates=_cell_gates(bundle.cells),
    )
    sealed = seal_evidence_input(validated, handoff, capabilities)
    assert sealed.intermediate_parameters == (a, b, c, d)
    assert all(
        cell.role is EvidenceCellRole.METHOD_INPUT
        for cell in sealed.cells
        if cell.parameter == c
    )
    assert all(gate.parameter == TARGET_PARAMETER for gate in sealed.gates)
    catalog = build_reasoning_evidence_catalog(
        sealed, tuple(fragment for _, fragment in built.validated_inputs.fragments)
    )
    skeleton = build_result_skeleton(
        validated, sealed, capabilities, result_id="saved-chain", generated_at=NOW
    )
    assert skeleton.requested_parameters == (TARGET_PARAMETER,)
    assert all(
        cell_id.startswith("cell:usgs:00300:") for cell_id in skeleton.cell_order
    )

    summaries = build_reasoning_evidence_summaries(handoff)
    working_context = build_working_context_projection(
        run_id=skeleton.run_id,
        session_id=skeleton.session_id,
        target_comid=skeleton.target_comid,
        scope_digest="sha256:pre-skeleton-scope",
        bundle_ref=skeleton.bundle_ref,
        bundle_digest=skeleton.bundle_digest,
        rule_plan_digest=skeleton.rule_plan_digest,
        capability_registry_digest=skeleton.capability_registry_digest,
        reference_allowlist=tuple(sorted({
            *skeleton.evidence_allowlist, *skeleton.source_record_allowlist
        })),
        recent_messages=(),
        facts=(),
        evidence_summaries=summaries,
    )
    reasoning_plan = _plan(
        skeleton, context=working_context, evidence_summaries=summaries
    )
    assert len(reasoning_plan.batch_plan.batches) == 1
    batch = reasoning_plan.batch_plan.batches[0]
    fragments = dict(built.validated_inputs.fragments)
    target_by_range = {
        fragment.time_range: reference
        for reference, fragment in built.validated_inputs.fragments
        if isinstance(fragment.payload, NumericDerivedPayload)
        and fragment.parameter_refs == (TARGET_PARAMETER,)
    }
    flow_by_range = {fragment.time_range: reference for reference, fragment in flow}
    facts = {fact.evidence_ref: fact for fact in catalog.facts}
    cells = []
    for planned in batch.cells:
        window_range = next(
            window.time_range for window in scope.time_windows
            if window.window_id == planned.window_id
        )
        target_ref = target_by_range[window_range]
        flow_ref = flow_by_range[window_range]
        target_fragment = fragments[target_ref]
        cited = (target_ref.evidence_id, flow_ref.evidence_id)
        assert set(cited) <= set(planned.allowed_evidence_refs)
        cells.append({
            "cell_id": planned.cell_id,
            "outcome_status": "quantified",
            "representative_value": target_fragment.payload.value,
            "target_unit_id": target_fragment.payload.unit,
            "source": "inferred",
            "trend": "stable",
            "risk_level": "none",
            "risk_rationale": "The saved calculation and discharge support this value.",
            "confidence": 0.5,
            "confidence_rationale": "The result depends on saved inferred values.",
            "aggregation_method": None,
            "evidence_refs": list(cited),
            "series_refs": sorted({
                facts[ref].series_ref for ref in cited
                if facts[ref].series_ref is not None
            }),
            "reason_codes": [],
            "quality_flags": sorted({
                flag.value for ref in cited for flag in facts[ref].quality_flags
            }),
            "explanation": "The target uses the saved intermediate chain.",
        })
    draft = {
        "schema_version": 1,
        "decision_type": "reasoning_batch",
        "batch_id": batch.batch_id,
        "scope_digest": batch.scope_digest,
        "cells": cells,
    }
    accepted = DraftValidator(catalog).validate_batch(batch, draft)
    assert len(accepted.cells) == len(scope.time_windows)
    assert all(cell.source.value == "inferred" for cell in accepted.cells)
    changed = copy.deepcopy(draft)
    changed["cells"][0]["representative_value"] = "999"
    with pytest.raises(DraftValidationError) as rejected:
        DraftValidator(catalog).validate_batch(batch, changed)
    assert "quantified_value_not_backed" in {
        issue.code for issue in rejected.value.issues
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
