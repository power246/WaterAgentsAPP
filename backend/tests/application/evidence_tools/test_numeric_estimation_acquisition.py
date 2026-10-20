import asyncio
from collections import Counter
from datetime import timedelta

import pytest
from app.application.evidence_tools.errors import EvidenceToolBudgetExceededError
from app.application.evidence_tools.numeric_estimation import NumericEstimateResult
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
    create_production_numeric_method_registry,
)
from app.application.reasoning.numeric_estimation import ModelNumericEstimator
from app.domain import CollectionFactPayload, CollectionStatus, NumericDerivedPayload
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import PortUnavailableError
from tests.application.evidence_tools.conftest import (
    MODEL,
    SUPPORT_PARAMETER,
    TARGET_PARAMETER,
    make_scope,
)
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
)
from tests.application.evidence_tools.test_acquisition_workflow import (
    _runtime,
    _water_port,
)
from tests.application.reasoning.test_numeric_estimation import EstimatePort


def _estimator(port, handoff, *, max_calls=64):
    clock = FixedClock()
    return ModelNumericEstimator(
        model_port=port, model=MODEL, bundle_ref=handoff.bundle_ref,
        capability_registry=load_parameter_capability_registry(),
        clock=clock, deadline=clock.now() + timedelta(minutes=5), max_calls=max_calls,
    )


def test_provider_failure_remains_visible_while_model_range_can_complete():
    async def run():
        def unavailable(_request):
            raise PortUnavailableError("provider is temporarily unavailable")
        scope = make_scope()
        store = MemoryEvidenceStore()
        service = _runtime(store, water_port=FakeCollectionPort(unavailable))
        prior = await service.start(scope)
        assert prior.numeric_estimation_allowed
        assert "evidence_provider_unavailable" in prior.block_facts
        port = EstimatePort()
        handoff = await service.resolve_numeric(scope, prior, estimator=_estimator(port, prior))
        fragments = [fragment for _, fragment in handoff.validated_inputs.fragments]
        assert port.calls
        assert any(isinstance(fragment.payload, NumericDerivedPayload) for fragment in fragments)
        failures = [fragment for fragment in fragments if isinstance(fragment.payload, CollectionFactPayload)
                    and fragment.payload.reason == "evidence_provider_unavailable"]
        assert failures and all(fragment.collection_status is CollectionStatus.UNAVAILABLE for fragment in failures)
        assert "evidence_provider_unavailable" in handoff.block_facts
        assert "evidence_provider_unavailable" in port.calls[0].messages[0].text
    asyncio.run(run())


def test_collection_budget_failure_prevents_estimation_dispatch():
    async def run():
        def exhausted(_request):
            raise EvidenceToolBudgetExceededError()
        scope = make_scope()
        store = MemoryEvidenceStore()
        service = _runtime(store, water_port=FakeCollectionPort(exhausted))
        prior = await service.start(scope)
        assert not prior.numeric_estimation_allowed
        port = EstimatePort()
        assert await service.resolve_numeric(scope, prior, estimator=_estimator(port, prior)) is prior
        assert port.calls == []
    asyncio.run(run())


def test_model_budget_exhaustion_is_persisted_as_technical_failure():
    async def run():
        scope = make_scope()
        store = MemoryEvidenceStore()
        service = _runtime(store, water_port=_water_port(scope, mode="empty"))
        prior = await service.start(scope)
        port = EstimatePort()
        estimator = _estimator(port, prior, max_calls=1)
        handoff = await service.resolve_numeric(scope, prior, estimator=estimator)
        assert estimator.calls == len(port.calls) == 1
        assert "numeric_estimation_call_budget_exceeded" in handoff.block_facts
        failures = [fragment for _, fragment in handoff.validated_inputs.fragments
                    if isinstance(fragment.payload, CollectionFactPayload)
                    and fragment.payload.reason == "numeric_estimation_call_budget_exceeded"]
        assert failures
        assert all(fragment.collection_status is CollectionStatus.UNAVAILABLE for fragment in failures)
    asyncio.run(run())


@pytest.mark.parametrize("abstain_flow", [False, True])
def test_range_or_abstention_predecessor_falls_back_once_to_target_estimation(abstain_flow):
    async def run():
        capabilities = load_parameter_capability_registry()
        base = create_production_numeric_method_registry(capabilities)
        flow = capabilities.get(SUPPORT_PARAMETER)
        target = capabilities.get(TARGET_PARAMETER)
        called_formula = []
        def formula(values, ranges, target_range):
            called_formula.append(values)
            return values[0]
        method = NumericMethod(
            method_id="target_from_flow_fixture", version=1,
            output_parameter=TARGET_PARAMETER, output_unit=target.canonical_unit,
            inputs=(NumericMethodInput(SUPPORT_PARAMETER, flow.canonical_unit),),
            calculate=formula, confidence_cap=0.5,
            allowed_modes=("gap_filling", "prediction"),
        )
        registry = NumericMethodRegistry((*base.methods, method))
        scope = make_scope()
        store = MemoryEvidenceStore()
        service = _runtime(store, water_port=_water_port(scope, mode="empty"), numeric_registry=registry)
        scope = service.prepare_scope(scope)
        prior = await service.start(scope)
        calls = Counter()
        async def estimate(request):
            calls[(request.task.output_parameter, request.task.window_id)] += 1
            abstain = abstain_flow and request.task.output_parameter == SUPPORT_PARAMETER
            return NumericEstimateResult(
                value=None, value_lower=None if abstain else "1", value_upper=None if abstain else "3",
                confidence=0.2, rationale="Range estimate or explicit lack of useful flow basis.",
                model_provider="test", model_name="estimate", operation_id="operation-test", prompt_digest="digest",
            )
        handoff = await service.resolve_numeric(scope, prior, estimator=estimate)
        assert not called_formula
        assert all(count == 1 for count in calls.values())
        estimates = [fragment for _, fragment in handoff.validated_inputs.fragments
                     if isinstance(fragment.payload, NumericDerivedPayload)
                     and fragment.parameter_refs == (TARGET_PARAMETER,)]
        assert len(estimates) == len(scope.time_windows)
        assert all(fragment.payload.method_kind == "model_estimate" for fragment in estimates)
        assert not any(code.startswith("numeric_") for code in handoff.block_facts)
    asyncio.run(run())
