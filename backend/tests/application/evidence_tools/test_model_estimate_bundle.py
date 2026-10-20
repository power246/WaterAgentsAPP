import asyncio

import pytest

from app.application.evidence_tools import (
    EvidenceBundleBuilder,
    EvidenceCollectionService,
)
from app.application.evidence_tools.bundle_builder import (
    _numeric_payload_value_unit,
    _validate_model_derivation,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.numeric_dependency import (
    NumericDependencyExecutor,
    NumericDependencyPlanner,
)
from app.application.evidence_tools.numeric_estimation import NumericEstimateResult
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodRegistry,
)
from app.domain import (
    CollectionFactPayload,
    CollectionStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceCellStatus,
    EvidenceFragment,
    NumericDependencyPlan,
    NumericPlanInput,
    NumericPlanTask,
    ParameterKey,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.fakes import FixedClock, MemoryEvidenceStore
from tests.application.evidence_tools.test_bundle_builder import (
    collection,
    provider_batch,
)
from tests.application.evidence_tools.test_collection_service import observation


async def _estimate(scope, basis, *, failed_provider=False, required_flow=None,
                    approval="approved"):
    store = MemoryEvidenceStore()
    records = () if basis == "general_knowledge" else (
        observation(
            scope, 0,
            parameter=(scope.requested_parameters[0] if basis == "same_parameter_measurement"
                       else scope.supporting_parameters[0]),
            approval=approval,
        ),
    )
    artifacts = await EvidenceCollectionService(
        registry=load_parameter_capability_registry(), writer=store,
    ).process(
        scope, operation_id="model-context",
        collections=(collection(scope, provider_batch(scope, records)),),
    )
    sources = (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
    if failed_provider:
        for reference in sources:
            fragment = await store.read_fragment(scope, reference)
            if (isinstance(fragment.payload, CollectionFactPayload)
                    and fragment.parameter_refs == (scope.requested_parameters[0],)):
                values = fragment.model_dump(mode="python")
                values.update(
                    collection_status=CollectionStatus.UNAVAILABLE,
                    payload=fragment.payload.model_copy(update={
                        "status": CollectionStatus.UNAVAILABLE,
                        "reason": "provider_transport_error",
                    }),
                )
                store.fragments[(reference.item_id, reference.evidence_id)] = EvidenceFragment.model_validate(values)
    method = NumericMethod(
        method_id="test_estimate", version=1,
        output_parameter=scope.requested_parameters[0], output_unit="mg/L",
        inputs=(), calculate=None, confidence_cap=1,
        allowed_modes=(scope.time_windows[0].window_kind.value,), kind="model_estimate",
    )
    registry = NumericMethodRegistry((method,))
    bindings = ()
    if required_flow is not None:
        flow_ref = next(
            ref for ref in sources
            if isinstance(store.fragments[(ref.item_id, ref.evidence_id)].payload, DirectRecordPayload)
        )
        bindings = (NumericPlanInput(
            parameter=scope.supporting_parameters[0], unit="ft3/s",
            window_id=scope.time_windows[0].window_id,
            evidence_ref=flow_ref, required=required_flow,
        ),)
    task = NumericPlanTask(
        task_id="estimate-target", target_comid=scope.agent_comid,
        output_parameter=method.output_parameter, output_unit=method.output_unit,
        window_id=scope.time_windows[0].window_id, mode=method.allowed_modes[0],
        method_id=method.method_id, method_version=1,
        method_kind="model_estimate", inputs=bindings,
    )
    plan = NumericDependencyPlan.create(
        run_id=scope.run_id, session_id=scope.session_id,
        target_comid=scope.agent_comid, method_registry_digest=registry.manifest_digest,
        source_fragment_refs=sources, tasks=(task,),
    )

    async def estimator(request):
        return NumericEstimateResult(
            value="7.2" if request.measured_anchors else None,
            value_lower="6", value_upper="9", confidence=0.15,
            rationale="Estimate using the available context.",
            model_provider="test", model_name="fixture", operation_id="model-call",
            prompt_digest="prompt-digest",
        )

    outcome = await NumericDependencyExecutor(
        registry=registry, reader=store, writer=store, limiter=asyncio.Semaphore(1),
    ).run(scope, plan, estimator=estimator)
    assert not outcome.failed_task_ids and not outcome.blocked_task_ids
    reference = outcome.fragment_refs[0]
    builder = EvidenceBundleBuilder(reader=store, writer=store, numeric_registry=registry)
    return store, builder, (*sources, reference), reference


async def _build(builder, scope, references):
    return await builder.build(
        scope, fragment_refs=references, tool_result_refs=(), created_at=FixedClock().now(),
    )


@pytest.mark.parametrize("basis", ["same_parameter_measurement", "related_evidence", "general_knowledge"])
def test_saved_model_estimate_accepts_traceable_point_or_range(evidence_scope, basis):
    async def exercise():
        store, builder, references, estimated = await _estimate(evidence_scope, basis)
        built = await _build(builder, evidence_scope, references)
        fragment = await store.read_fragment(evidence_scope, estimated)
        assert fragment.payload.inference_basis == basis
        assert bool(fragment.payload.measured_anchors) == (basis == "same_parameter_measurement")
        assert (fragment.payload.value is not None) == (basis == "same_parameter_measurement")
        assert _numeric_payload_value_unit(fragment.payload) == (
            ("7.2", "mg/L") if basis == "same_parameter_measurement" else None
        )
        target = built.bundle.cells[0]
        assert target.status is EvidenceCellStatus.SUPPORTED
        assert estimated.evidence_id in target.allowed_indirect_fragment_refs
        assert fragment.payload.confidence_cap == 0.15

    asyncio.run(exercise())


def test_model_estimate_retains_source_quality_flags_and_rejects_stripping(
    evidence_scope,
):
    async def exercise():
        store, builder, references, estimated = await _estimate(
            evidence_scope, "same_parameter_measurement", approval="provisional"
        )
        fragment = await store.read_fragment(evidence_scope, estimated)
        assert DataQualityFlag.PROVIDER_FLAGGED in fragment.quality_flags
        assert DataQualityFlag.ESTIMATED in fragment.quality_flags
        await _build(builder, evidence_scope, references)

        stripped = fragment.model_copy(update={
            "quality_flags": tuple(
                flag for flag in fragment.quality_flags
                if flag is not DataQualityFlag.PROVIDER_FLAGGED
            ),
        })
        store.fragments[(estimated.item_id, estimated.evidence_id)] = stripped
        with pytest.raises(EvidenceScopeViolationError):
            await _build(builder, evidence_scope, references)

    asyncio.run(exercise())


@pytest.mark.parametrize("ph_code", ["00400", "00403"])
def test_bundle_rejects_uninformative_ph_range_for_both_capabilities(
    evidence_scope, ph_code
):
    async def exercise():
        store, _, references, estimated = await _estimate(
            evidence_scope, "general_knowledge"
        )
        fragment = await store.read_fragment(evidence_scope, estimated)
        plan = await store.read_numeric_plan(
            evidence_scope, fragment.payload.plan_item_ref
        )
        ph = ParameterKey(code_system="usgs", code=ph_code)
        task = plan.tasks[0].model_copy(update={"output_parameter": ph})
        saved = {
            reference.evidence_id: (
                reference, await store.read_fragment(evidence_scope, reference)
            )
            for reference in references[:-1]
        }

        def candidate(lower, upper):
            return EvidenceFragment.model_validate(fragment.model_copy(update={
                "parameter_refs": (ph,),
                "payload": fragment.payload.model_copy(update={
                    "value_lower": lower, "value_upper": upper,
                }),
            }).model_dump(mode="python"))

        _validate_model_derivation(
            evidence_scope, candidate("6", "8"), task, plan, saved
        )
        with pytest.raises(EvidenceScopeViolationError):
            _validate_model_derivation(
                evidence_scope, candidate("0", "14"), task, plan, saved
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("tamper", ["anchor_origin", "input_window", "unplanned_input"])
def test_bundle_rejects_fabricated_estimate_provenance(evidence_scope, tamper):
    async def exercise():
        store, builder, references, estimated = await _estimate(evidence_scope, "same_parameter_measurement")
        fragment = await store.read_fragment(evidence_scope, estimated)
        payload = fragment.payload.model_dump(mode="python")
        if tamper == "anchor_origin":
            anchor = fragment.payload.measured_anchors[0]
            payload["measured_anchors"] = (anchor.model_copy(update={
                "origin_comid": "999", "path_comids": ("999", evidence_scope.agent_comid),
                "distance_km": 100,
            }),)
        elif tamper == "input_window":
            payload["input_window_ids"] = ("context", *fragment.payload.input_window_ids[1:])
        else:
            original = await store.read_fragment(evidence_scope, references[0])
            extra = original.model_copy(update={"evidence_id": "unplanned-context"})
            extra_ref = await store.write_fragment(evidence_scope, operation_id="unplanned", fragment=extra)
            references = (*references, extra_ref)
            payload["input_evidence_refs"] = (*fragment.payload.input_evidence_refs, extra_ref)
            payload["input_window_ids"] = (*fragment.payload.input_window_ids, evidence_scope.time_windows[0].window_id)
        values = fragment.model_dump(mode="python")
        values["payload"] = payload
        store.fragments[(estimated.item_id, estimated.evidence_id)] = EvidenceFragment.model_validate(values)
        with pytest.raises(EvidenceScopeViolationError):
            await _build(builder, evidence_scope, references)

    asyncio.run(exercise())


@pytest.mark.parametrize("required", [True, False])
def test_only_required_inputs_prevent_sealing_when_missing(evidence_scope, required):
    async def exercise():
        store, builder, references, estimated = await _estimate(
            evidence_scope, "related_evidence", required_flow=required,
        )
        fragment = await store.read_fragment(evidence_scope, estimated)
        values = fragment.model_dump(mode="python")
        values["payload"].update(
            input_evidence_refs=(), input_window_ids=(), inference_basis="general_knowledge",
        )
        store.fragments[(estimated.item_id, estimated.evidence_id)] = EvidenceFragment.model_validate(values)
        if required:
            with pytest.raises(EvidenceScopeViolationError):
                await _build(builder, evidence_scope, references)
        else:
            await _build(builder, evidence_scope, references)

    asyncio.run(exercise())


def test_estimate_can_coexist_with_provider_failure_without_erasing_failure(evidence_scope):
    async def exercise():
        store, builder, references, estimated = await _estimate(
            evidence_scope, "general_knowledge", failed_provider=True,
        )
        without_estimate = await _build(builder, evidence_scope, references[:-1])
        assert without_estimate.bundle.cells[0].status is EvidenceCellStatus.BLOCKED
        with_estimate = await _build(builder, evidence_scope, references)
        target = with_estimate.bundle.cells[0]
        assert target.status is EvidenceCellStatus.SUPPORTED
        assert "provider_transport_error" in target.reasons
        assert target.collection_fact_refs
        assert estimated.evidence_id in target.allowed_indirect_fragment_refs
        assert (await store.read_fragment(evidence_scope, estimated)).payload.inference_basis == "general_knowledge"

    asyncio.run(exercise())


@pytest.mark.parametrize("flow_outcome", ["abstain", "technical_failure"])
def test_optional_flow_failure_settles_before_target_estimate_and_bundle(evidence_scope, flow_outcome):
    async def exercise():
        store, _, existing, _ = await _estimate(evidence_scope, "general_knowledge")
        sources = existing[:-1]
        flow = evidence_scope.supporting_parameters[0]
        for reference in sources:
            source = await store.read_fragment(evidence_scope, reference)
            if source.parameter_refs == (flow,):
                values = source.model_dump(mode="python")
                values.update(
                    collection_status=CollectionStatus.UNAVAILABLE,
                    payload=source.payload.model_copy(update={
                        "status": CollectionStatus.UNAVAILABLE,
                        "reason": "provider_transport_error",
                    }),
                )
                store.fragments[(reference.item_id, reference.evidence_id)] = EvidenceFragment.model_validate(values)
        registry = NumericMethodRegistry(tuple(
            NumericMethod(
                method_id=f"estimate_{parameter.code}", version=1,
                output_parameter=parameter,
                output_unit="ft3/s" if parameter == flow else "mg/L",
                inputs=(), calculate=None, confidence_cap=1,
                allowed_modes=(evidence_scope.time_windows[0].window_kind.value,),
                kind="model_estimate",
            )
            for parameter in (evidence_scope.requested_parameters[0], flow)
        ))
        plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
            evidence_scope, sources, allow_estimation=True,
        )
        flow_attempts = set()

        async def estimator(request):
            is_flow = request.task.output_parameter == flow
            if is_flow:
                flow_attempts.add(request.task.window_id)
                if flow_outcome == "technical_failure":
                    raise RuntimeError("test provider failure")
            else:
                assert request.task.window_id in flow_attempts
                assert request.input_fragments == ()
            return NumericEstimateResult(
                value=None,
                value_lower=None if is_flow else "6",
                value_upper=None if is_flow else "9",
                confidence=0 if is_flow else 0.15,
                rationale="No flow estimate available." if is_flow else "Related flow is unavailable.",
                model_provider="test", model_name="fixture", operation_id="flow-chain",
                prompt_digest="prompt-chain",
            )

        outcome = await NumericDependencyExecutor(
            registry=registry, reader=store, writer=store, limiter=asyncio.Semaphore(2),
        ).run(evidence_scope, plan, estimator=estimator)
        assert not outcome.blocked_task_ids
        assert bool(outcome.failed_task_ids) == (flow_outcome == "technical_failure")
        built = await _build(
            EvidenceBundleBuilder(reader=store, writer=store, numeric_registry=registry),
            evidence_scope, (*sources, *outcome.fragment_refs),
        )
        for cell in built.bundle.cells:
            if cell.parameter == flow:
                assert cell.status is EvidenceCellStatus.BLOCKED
                assert "provider_transport_error" in cell.reasons
                assert cell.collection_fact_refs
            else:
                assert cell.status is EvidenceCellStatus.SUPPORTED
                assert cell.allowed_indirect_fragment_refs

    asyncio.run(exercise())
