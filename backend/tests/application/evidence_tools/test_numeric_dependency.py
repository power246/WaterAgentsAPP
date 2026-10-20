import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application.analysis_runs.workflow import CancellationToken, WorkflowStopped
from app.application.evidence_tools.acquisition import EvidenceAcquisitionService
from app.application.evidence_tools.bundle_builder import EvidenceBundleBuilder
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.numeric_dependency import (
    NumericDependencyExecutor,
    NumericDependencyPlanError,
    NumericDependencyPlanner,
    _temporal_task,
    prepare_numeric_scope,
)
from app.application.evidence_tools.numeric_estimation import NumericEstimateResult
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
    create_production_numeric_method_registry,
)
from app.application.evidence_tools.scope import (
    EvidenceWindow,
    with_intermediate_parameters,
)
from app.application.evidence_tools.temporal_candidate import (
    build_temporal_fragment,
    select_temporal_candidates,
)
from app.application.transcripts.models import StoredTranscriptItem
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    AggregatePayload,
    CollectionFactPayload,
    CollectionStatus,
    ContextPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    InferenceMode,
    NumericDependencyPlan,
    NumericDerivedPayload,
    NumericPlanTask,
    ParameterKey,
    Provenance,
    SourceRecordReference,
    TemporalEstimatePayload,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import make_context
from tests.application.evidence_tools.fakes import FixedClock, MemoryEvidenceStore
from tests.application.evidence_tools.test_collection_service import observation
from tests.application.evidence_tools.test_temporal_candidate import _evidence


def _key(code):
    return ParameterKey(code_system="usgs", code=code)


SOURCE_A, SOURCE_B, SOURCE_D = _key("90001"), _key("90002"), _key("90003")
A, B, C, D, T, U = (
    _key("90011"),
    _key("90012"),
    _key("90013"),
    _key("90014"),
    _key("00300"),
    _key("90016"),
)


def _scope(scope_factory, *, targets=(T,)):
    original = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
    )
    return SimpleNamespace(
        run_id=original.run_id,
        session_id=original.session_id,
        agent_comid=original.agent_comid,
        target_node=original.target_node,
        time_windows=(original.time_windows[0],),
        requested_parameters=targets,
        supporting_parameters=(),
        intermediate_parameters=(),
        evidence_rule_plan=original.evidence_rule_plan,
        cancel_token=original.cancel_token,
    )


def _method(name, output, inputs, calculate):
    return NumericMethod(
        method_id=f"test_{name}",
        version=1,
        output_parameter=output,
        output_unit="unit",
        inputs=tuple(
            NumericMethodInput(parameter=item, unit="unit") for item in inputs
        ),
        calculate=calculate,
        confidence_cap=0.6,
    )


def _registry(*, shared_second_target=False):
    identity = lambda values, _windows, _target: values[0]
    add = lambda values, _windows, _target: sum(values, Decimal(0))
    methods = [
        _method("a", A, (SOURCE_A,), identity),
        _method("b", B, (SOURCE_B,), identity),
        _method("c", C, (A, B), add),
        _method("d", D, (SOURCE_D,), identity),
        _method("t", T, (C, D), add),
    ]
    if shared_second_target:
        methods.append(_method("u", U, (C,), identity))
    return NumericMethodRegistry(tuple(methods))


def _seed(store, scope, parameter, value, *, source_record=None):
    source_ref = SourceRecordReference(
        item_id=str(uuid4()),
        record_id=(
            source_record.record_id
            if source_record is not None
            else f"record-{parameter.code}"
        ),
    )
    fragment = EvidenceFragment(
        evidence_id=f"seed-{parameter.code}",
        source="test_observation",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid=scope.agent_comid,
        location=EvidenceLocation(
            point=scope.target_node.point,
            comid=scope.agent_comid,
            station_id=None,
            distance_to_target_km=0,
        ),
        time_range=scope.time_windows[0].time_range,
        parameter_refs=(parameter,),
        data_type="direct_record",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity=f"test-series-{parameter.code}",
        quality_class=EvidenceQualityClass.VALID,
        coverage=EvidenceCoverage(
            status=CoverageStatus.PARTIAL,
            observed_count=1,
            covered_duration_seconds=0,
            window_duration_seconds=21600,
        ),
        payload=DirectRecordPayload(
            record_ref=source_ref,
            displayed_value=value,
            displayed_unit="unit",
        ),
        source_record_refs=(source_ref,),
        provenance=Provenance(operation_id=f"seed-{parameter.code}"),
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
    )
    reference = EvidenceFragmentReference(
        item_id=str(uuid4()), evidence_id=fragment.evidence_id
    )
    store.seed_fragment(reference, fragment)
    store.seed_scope(
        reference.item_id, run_id=scope.run_id, session_id=scope.session_id
    )
    if source_record is not None:
        store.seed_records(scope, source_ref.item_id, (source_record,))
    return reference


class GatedStore(MemoryEvidenceStore):
    def __init__(self):
        super().__init__()
        self.started = {name: asyncio.Event() for name in "ABCDT"}
        self.release = {name: asyncio.Event() for name in "ABCDT"}
        self.saved = {name: asyncio.Event() for name in "ABCDT"}
        self.writes = []

    async def write_fragment(self, scope, *, operation_id, fragment):
        if isinstance(fragment.payload, NumericDerivedPayload):
            name = fragment.payload.method_id.removeprefix("test_").upper()
            self.started[name].set()
            if name == "C":
                assert self.saved["A"].is_set() and self.saved["B"].is_set()
            if name == "T":
                assert self.saved["C"].is_set() and self.saved["D"].is_set()
            await self.release[name].wait()
            reference = await super().write_fragment(
                scope, operation_id=operation_id, fragment=fragment
            )
            self.writes.append(name)
            self.saved[name].set()
            return reference
        return await super().write_fragment(
            scope, operation_id=operation_id, fragment=fragment
        )


def test_scheduler_waits_for_saved_predecessors_and_reads_numeric_values(
    scope_factory,
):
    asyncio.run(_scheduler_waits_for_saved_predecessors(scope_factory))


async def _scheduler_waits_for_saved_predecessors(
    scope_factory,
):
    scope = _scope(scope_factory)
    store = GatedStore()
    refs = tuple(
        _seed(store, scope, parameter, value)
        for parameter, value in ((SOURCE_A, "1"), (SOURCE_B, "2"), (SOURCE_D, "4"))
    )
    registry = _registry()
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, refs
    )
    assert len(plan.tasks) == 5
    execution = asyncio.create_task(
        NumericDependencyExecutor(
            registry=registry,
            reader=store,
            writer=store,
            limiter=asyncio.Semaphore(3),
            max_concurrency=3,
        ).run(scope, plan)
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(*(store.started[name].wait() for name in "ABD")), timeout=2
        )
        store.release["A"].set()
        await asyncio.wait_for(store.saved["A"].wait(), timeout=2)
        assert not store.started["C"].is_set()
        store.release["B"].set()
        await asyncio.wait_for(store.started["C"].wait(), timeout=2)
        assert not store.started["T"].is_set()
        store.release["C"].set()
        await asyncio.wait_for(store.saved["C"].wait(), timeout=2)
        assert not store.started["T"].is_set()
        store.release["D"].set()
        await asyncio.wait_for(store.started["T"].wait(), timeout=2)
        store.release["T"].set()
        outcome = await asyncio.wait_for(execution, timeout=2)
    finally:
        for event in store.release.values():
            event.set()
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
    assert not outcome.blocked_task_ids and not outcome.failed_task_ids
    assert sorted(store.writes) == sorted("ABCDT")
    derived = {
        fragment.payload.method_id: fragment
        for fragment in store.fragments.values()
        if isinstance(fragment.payload, NumericDerivedPayload)
    }
    target = derived["test_t"]
    assert target.payload.value == "7"
    assert target.payload.plan_item_ref == outcome.plan_ref
    assert tuple(
        store.fragments[(reference.item_id, reference.evidence_id)].payload.value
        for reference in target.payload.input_evidence_refs
    ) == ("3", "4")


def test_shared_intermediate_is_planned_and_written_once(scope_factory):
    asyncio.run(_shared_intermediate_is_planned_and_written_once(scope_factory))


async def _shared_intermediate_is_planned_and_written_once(scope_factory):
    scope = _scope(scope_factory, targets=(T, U))
    store = MemoryEvidenceStore()
    refs = tuple(
        _seed(store, scope, parameter, value)
        for parameter, value in ((SOURCE_A, "1"), (SOURCE_B, "2"), (SOURCE_D, "4"))
    )
    registry = _registry(shared_second_target=True)
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, refs
    )
    assert len(plan.tasks) == 6
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(3),
        max_concurrency=3,
    ).run(scope, plan)
    assert not outcome.failed_task_ids
    assert (
        sum(
            isinstance(fragment.payload, NumericDerivedPayload)
            and fragment.payload.method_id == "test_c"
            for fragment in store.fragments.values()
        )
        == 1
    )


def test_same_window_cycle_rejected_before_writes(scope_factory):
    asyncio.run(_same_window_cycle_rejected_before_writes(scope_factory))


async def _same_window_cycle_rejected_before_writes(scope_factory):
    scope = _scope(scope_factory, targets=(A,))
    store = MemoryEvidenceStore()
    registry = NumericMethodRegistry(
        (
            _method("a", A, (B,), lambda values, _windows, _target: values[0]),
            _method("b", B, (A,), lambda values, _windows, _target: values[0]),
        )
    )
    with pytest.raises(NumericDependencyPlanError, match="cycle"):
        await NumericDependencyPlanner(registry=registry, reader=store).plan(scope, ())
    assert not store.numeric_plans and not store.fragments


def test_planner_chooses_compatible_unit_and_available_method(scope_factory):
    asyncio.run(_planner_chooses_compatible_unit_and_available_method(scope_factory))


async def _planner_chooses_compatible_unit_and_available_method(scope_factory):
    scope = _scope(scope_factory)
    store = MemoryEvidenceStore()
    wrong = _seed(store, scope, SOURCE_A, "1")
    wrong_key = (wrong.item_id, wrong.evidence_id)
    wrong_fragment = store.fragments[wrong_key]
    store.fragments[wrong_key] = wrong_fragment.model_copy(
        update={
            "payload": wrong_fragment.payload.model_copy(
                update={"displayed_unit": "wrong"}
            )
        }
    )
    valid = _seed(store, scope, SOURCE_A, "2")
    registry = NumericMethodRegistry(
        (
            _method(
                "first_missing",
                T,
                (SOURCE_B,),
                lambda values, _windows, _target: values[0],
            ),
            _method(
                "second_available",
                T,
                (SOURCE_A,),
                lambda values, _windows, _target: values[0],
            ),
        )
    )
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, (wrong, valid)
    )
    assert len(plan.tasks) == 1
    assert plan.tasks[0].method_id == "test_second_available"
    assert plan.tasks[0].inputs[0].evidence_ref == valid
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(2),
    ).run(scope, plan)
    assert not outcome.failed_task_ids
    target = await store.read_fragment(scope, outcome.fragment_refs[0])
    assert target.payload.value == "2"


def test_cross_window_chain_uses_saved_start_and_rejects_same_cell_cycle(scope_factory):
    asyncio.run(_cross_window_chain_uses_saved_start(scope_factory))


async def _cross_window_chain_uses_saved_start(scope_factory):
    original = _scope(scope_factory)
    full = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
    )
    scope = SimpleNamespace(
        **{
            name: getattr(original, name)
            for name in (
                "run_id",
                "session_id",
                "agent_comid",
                "target_node",
                "cancel_token",
                "supporting_parameters",
                "intermediate_parameters",
            )
        }
    )
    scope.time_windows = full.time_windows[:3]
    scope.requested_parameters = (A,)
    store = MemoryEvidenceStore()
    start = _seed(store, scope, A, "5")
    registry = NumericMethodRegistry(
        (
            NumericMethod(
                method_id="test_a_from_prior_b",
                version=1,
                output_parameter=A,
                output_unit="unit",
                inputs=(
                    NumericMethodInput(
                        parameter=B, unit="unit", window_relation="nearest_before"
                    ),
                ),
                calculate=lambda values, _windows, _target: values[0] + 1,
                confidence_cap=0.6,
                production=True,
            ),
            NumericMethod(
                method_id="test_b_from_prior_a",
                version=1,
                output_parameter=B,
                output_unit="unit",
                inputs=(
                    NumericMethodInput(
                        parameter=A, unit="unit", window_relation="nearest_before"
                    ),
                ),
                calculate=lambda values, _windows, _target: values[0] + 1,
                confidence_cap=0.6,
                production=True,
            ),
        )
    )
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, (start,)
    )
    assert len(plan.tasks) == 2
    by_cell = {(task.output_parameter, task.window_id): task for task in plan.tasks}
    second = by_cell[(B, scope.time_windows[1].window_id)]
    third = by_cell[(A, scope.time_windows[2].window_id)]
    assert second.inputs[0].evidence_ref == start
    assert third.inputs[0].dependency_task_id == second.task_id
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(2),
    ).run(scope, plan)
    assert not outcome.failed_task_ids
    values = {
        (fragment.parameter_refs[0], fragment.time_range): fragment.payload.value
        for reference in outcome.fragment_refs
        for fragment in (await store.read_fragment(scope, reference),)
    }
    assert values[(B, scope.time_windows[1].time_range)] == "6"
    assert values[(A, scope.time_windows[2].time_range)] == "7"


def test_prepare_scope_unions_selected_methods_for_mixed_window_modes(
    monkeypatch, scope_factory
):
    full = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
    )
    history = full.time_windows[0]
    prediction = EvidenceWindow(
        window_id="synthetic-prediction",
        time_range=full.time_windows[1].time_range,
        segment_index=1,
        window_kind=InferenceMode.PREDICTION,
    )
    scope = SimpleNamespace(
        requested_parameters=(T,),
        supporting_parameters=(),
        time_windows=(history, prediction),
    )
    registry = NumericMethodRegistry(
        (
            replace(
                _method(
                    "history", T, (A,), lambda values, _windows, _target: values[0]
                ),
                allowed_modes=(InferenceMode.GAP_FILLING.value,),
            ),
            replace(
                _method(
                    "prediction", T, (B,), lambda values, _windows, _target: values[0]
                ),
                allowed_modes=(InferenceMode.PREDICTION.value,),
            ),
        )
    )
    monkeypatch.setattr(
        "app.application.evidence_tools.numeric_dependency.with_intermediate_parameters",
        lambda _scope, parameters: parameters,
    )
    assert set(prepare_numeric_scope(scope, registry)) == {A, B}


def test_failed_write_blocks_only_descendants_and_marks_technical_cells(scope_factory):
    asyncio.run(_failed_write_blocks_only_descendants(scope_factory))


async def _failed_write_blocks_only_descendants(scope_factory):
    class FailingStore(MemoryEvidenceStore):
        async def write_fragment(self, scope, *, operation_id, fragment):
            if (
                isinstance(fragment.payload, NumericDerivedPayload)
                and fragment.payload.method_id == "test_c"
            ):
                raise OSError("injected write failure")
            return await super().write_fragment(
                scope, operation_id=operation_id, fragment=fragment
            )

    scope = _scope(scope_factory)
    store = FailingStore()
    refs = tuple(
        _seed(store, scope, parameter, value)
        for parameter, value in ((SOURCE_A, "1"), (SOURCE_B, "2"), (SOURCE_D, "4"))
    )
    registry = _registry()
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, refs
    )
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(3),
    ).run(scope, plan)
    by_id = {task.task_id: task for task in plan.tasks}
    assert {by_id[item].method_id for item in outcome.failed_task_ids} == {"test_c"}
    assert {by_id[item].method_id for item in outcome.blocked_task_ids} == {"test_t"}
    assert {
        (await store.read_fragment(scope, ref)).payload.method_id
        for ref in outcome.fragment_refs
    } == {"test_a", "test_b", "test_d"}

    service = object.__new__(EvidenceAcquisitionService)
    service._reader = store
    service._writer = store
    affected = tuple(
        (
            (task.output_parameter, task.window_id),
            "numeric_dependency_failed"
            if task.task_id in outcome.failed_task_ids
            else "numeric_dependency_blocked",
        )
        for task in plan.tasks
        if task.task_id in {*outcome.failed_task_ids, *outcome.blocked_task_ids}
    )
    facts = await service._write_numeric_failure_facts(
        scope, affected, (*refs, *outcome.fragment_refs)
    )
    assert len(facts) == 2
    reasons = {
        (await store.read_fragment(scope, ref)).parameter_refs[0]: (
            await store.read_fragment(scope, ref)
        ).payload.reason
        for ref in facts
    }
    assert reasons == {C: "numeric_dependency_failed", T: "numeric_dependency_blocked"}


def test_invalid_direct_record_does_not_hide_numeric_failure(scope_factory):
    asyncio.run(_invalid_direct_record_does_not_hide_numeric_failure(scope_factory))


async def _invalid_direct_record_does_not_hide_numeric_failure(scope_factory):
    scope = _scope(scope_factory)
    store = MemoryEvidenceStore()
    invalid_ref = _seed(store, scope, T, "7")
    original = await store.read_fragment(scope, invalid_ref)
    invalid = EvidenceFragment.model_validate(
        original.model_copy(
            update={
                "quality_class": EvidenceQualityClass.INVALID,
                "quality_flags": (DataQualityFlag.UNIT_UNRESOLVED,),
                "result_eligibility": EvidenceResultEligibility.EXPLANATORY_ONLY,
            }
        ).model_dump(mode="python")
    )
    store.seed_fragment(invalid_ref, invalid)
    service = object.__new__(EvidenceAcquisitionService)
    service._reader = store
    service._writer = store
    affected = (((T, scope.time_windows[0].window_id), "numeric_dependency_failed"),)
    facts = await service._write_numeric_failure_facts(scope, affected, (invalid_ref,))
    assert len(facts) == 1
    fact = await store.read_fragment(scope, facts[0])
    assert fact.collection_status is CollectionStatus.UNAVAILABLE
    assert fact.payload.reason == "numeric_dependency_failed"

    valid_ref = _seed(store, scope, T, "8")
    assert (
        await service._write_numeric_failure_facts(
            scope, affected, (invalid_ref, valid_ref)
        )
        == ()
    )


def test_numeric_scheduler_deadline_cancels_pending_plan_write(scope_factory):
    asyncio.run(_numeric_scheduler_deadline_cancels_pending_plan_write(scope_factory))


async def _numeric_scheduler_deadline_cancels_pending_plan_write(scope_factory):
    class HangingStore(MemoryEvidenceStore):
        async def write_numeric_plan(self, scope, plan):
            await asyncio.Event().wait()

    scope = _scope(scope_factory)
    store = HangingStore()
    source = _seed(store, scope, SOURCE_A, "1")
    registry = NumericMethodRegistry(
        (
            _method(
                "target", T, (SOURCE_A,), lambda values, _windows, _target: values[0]
            ),
        )
    )
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, (source,)
    )
    with pytest.raises(TimeoutError):
        await NumericDependencyExecutor(
            registry=registry,
            reader=store,
            writer=store,
            limiter=asyncio.Semaphore(1),
            total_deadline_seconds=0.01,
        ).run(scope, plan)
    assert not store.numeric_plans


def test_cancellation_stops_running_numeric_tasks(scope_factory):
    asyncio.run(_cancellation_stops_running_numeric_tasks(scope_factory))


async def _cancellation_stops_running_numeric_tasks(scope_factory):
    scope = _scope(scope_factory)
    event = asyncio.Event()
    scope.cancel_token = CancellationToken(event)
    store = GatedStore()
    source = _seed(store, scope, SOURCE_A, "1")
    registry = NumericMethodRegistry(
        (
            _method("a", A, (SOURCE_A,), lambda values, _windows, _target: values[0]),
            _method("t", T, (A,), lambda values, _windows, _target: values[0]),
        )
    )
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, (source,)
    )
    running = asyncio.create_task(
        NumericDependencyExecutor(
            registry=registry,
            reader=store,
            writer=store,
            limiter=asyncio.Semaphore(1),
        ).run(scope, plan)
    )
    await asyncio.wait_for(store.started["A"].wait(), timeout=2)
    event.set()
    with pytest.raises(WorkflowStopped):
        await asyncio.wait_for(running, timeout=2)
    assert not any(
        isinstance(fragment.payload, NumericDerivedPayload)
        for fragment in store.fragments.values()
    )


def test_bundle_validates_saved_numeric_chain_and_rejects_tampering(scope_factory):
    asyncio.run(
        _bundle_validates_saved_numeric_chain_and_rejects_tampering(scope_factory)
    )


async def _bundle_validates_saved_numeric_chain_and_rejects_tampering(scope_factory):
    scope = with_intermediate_parameters(
        scope_factory(
            context=make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
        ),
        (SOURCE_A, SOURCE_B, SOURCE_D, A, B, C, D),
    )
    store = MemoryEvidenceStore()
    refs = tuple(
        _seed(
            store,
            scope,
            parameter,
            value,
            source_record=observation(scope, index, parameter=parameter).model_copy(
                update={"value": value}
            ),
        )
        for index, (parameter, value) in enumerate(
            ((SOURCE_A, "1"), (SOURCE_B, "2"), (SOURCE_D, "4"))
        )
    )
    registry = _registry()
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, refs
    )
    assert len(plan.tasks) == 5
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(3),
        max_concurrency=3,
    ).run(scope, plan)
    assert not outcome.failed_task_ids
    all_refs = (*refs, *outcome.fragment_refs)
    builder = EvidenceBundleBuilder(
        reader=store, writer=store, numeric_registry=registry
    )
    await builder.build(
        scope,
        fragment_refs=all_refs,
        tool_result_refs=(),
        created_at=FixedClock().now(),
    )
    target_ref = next(
        reference
        for reference in outcome.fragment_refs
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (T,)
    )
    key = (target_ref.item_id, target_ref.evidence_id)
    original = store.fragments[key]
    wrong_window = scope.time_windows[1].time_range
    alternatives = {
        "value": original.model_copy(
            update={"payload": original.payload.model_copy(update={"value": "8"})}
        ),
        "unit": original.model_copy(
            update={"payload": original.payload.model_copy(update={"unit": "other"})}
        ),
        "window": original.model_copy(update={"time_range": wrong_window}),
        "reference": original.model_copy(
            update={
                "payload": original.payload.model_copy(
                    update={
                        "input_evidence_refs": (
                            refs[0],
                            original.payload.input_evidence_refs[1],
                        )
                    }
                )
            }
        ),
        "plan_digest": original.model_copy(
            update={
                "payload": original.payload.model_copy(update={"plan_digest": "0" * 64})
            }
        ),
    }
    for mutated in alternatives.values():
        store.fragments[key] = mutated
        with pytest.raises(EvidenceScopeViolationError):
            await builder.build(
                scope,
                fragment_refs=all_refs,
                tool_result_refs=(),
                created_at=FixedClock().now(),
            )
        store.fragments[key] = original


def test_temporal_tasks_feed_numeric_tasks_through_persisted_plan_and_bundle(
    scope_factory,
):
    asyncio.run(_temporal_tasks_feed_numeric_tasks(scope_factory))


def test_multiple_temporal_series_execute_only_the_selected_path(scope_factory):
    asyncio.run(_multiple_temporal_series_execute_only_the_selected_path(scope_factory))


async def _multiple_temporal_series_execute_only_the_selected_path(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    capabilities, original_refs, original_fragments = _evidence(scope)
    store = MemoryEvidenceStore()
    for reference, fragment in zip(original_refs, original_fragments, strict=True):
        store.seed_fragment(reference, fragment)

        def alternate_record_ref(source):
            return SourceRecordReference(
                item_id=f"alternate-{source.item_id}",
                record_id=f"alternate-{source.record_id}",
            )

        payload = fragment.payload
        if isinstance(payload, DirectRecordPayload):
            payload = payload.model_copy(
                update={"record_ref": alternate_record_ref(payload.record_ref)}
            )
        elif isinstance(payload, AggregatePayload):
            payload = payload.model_copy(
                update={
                    "input_record_refs": tuple(
                        alternate_record_ref(item) for item in payload.input_record_refs
                    ),
                }
            )
        elif isinstance(payload, CollectionFactPayload):
            payload = payload.model_copy(
                update={
                    "record_refs": tuple(
                        alternate_record_ref(item) for item in payload.record_refs
                    ),
                }
            )
        alternate = fragment.model_copy(
            update={
                "evidence_id": f"alternate-{fragment.evidence_id}",
                "series_identity": "series-456",
                "provenance": fragment.provenance.model_copy(
                    update={"operation_id": "collect-alternate"}
                ),
                "payload": payload,
                "source_record_refs": tuple(
                    alternate_record_ref(item) for item in fragment.source_record_refs
                ),
            }
        )
        store.seed_fragment(
            EvidenceFragmentReference(
                item_id=f"alternate-{reference.item_id}",
                evidence_id=alternate.evidence_id,
            ),
            alternate,
        )
    refs = tuple(
        EvidenceFragmentReference(item_id=item_id, evidence_id=evidence_id)
        for item_id, evidence_id in store.fragments
    )
    fragments = await store.read_fragments(scope, refs)
    candidates = select_temporal_candidates(scope, refs, fragments, capabilities)
    assert len(candidates) == 4
    assert {
        candidate.series_identity for candidate in candidates
        if candidate.window_id == "window-001"
    } == {"series-123", "series-456"}

    registry = create_production_numeric_method_registry(capabilities)
    plan = await NumericDependencyPlanner(
        registry=registry,
        reader=store,
        capability_registry=capabilities,
    ).plan(scope, refs)
    assert len(plan.tasks) == 2
    assert {task.window_id for task in plan.tasks} == {
        "window-001", "window-003"
    }
    assert {task.series_identity for task in plan.tasks} == {"series-123"}
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(2),
        capability_registry=capabilities,
    ).run(scope, plan)
    assert not outcome.failed_task_ids and not outcome.blocked_task_ids
    assert len(outcome.fragment_refs) == 2
    produced = await store.read_fragments(scope, outcome.fragment_refs)
    assert all(
        isinstance(fragment.payload, TemporalEstimatePayload)
        for fragment in produced
    )


async def _temporal_tasks_feed_numeric_tasks(scope_factory):
    scope = scope_factory(
        context=make_context(
            start_date=date(2026, 9, 28),
            end_date=date(2026, 9, 29),
            decision_instant=datetime(2026, 10, 2, tzinfo=UTC),
        )
    )
    capabilities, refs, fragments = _evidence(scope)
    store = MemoryEvidenceStore()
    for reference, fragment in zip(refs, fragments, strict=True):
        store.seed_fragment(reference, fragment)
        store.seed_scope(
            reference.item_id, run_id=scope.run_id, session_id=scope.session_id
        )
        if isinstance(fragment.payload, DirectRecordPayload):
            source_ref = fragment.payload.record_ref
            window_index = next(
                index
                for index, window in enumerate(scope.time_windows)
                if window.time_range == fragment.time_range
            )
            original = observation(
                scope,
                window_index,
                parameter=scope.requested_parameters[0],
                window_index=window_index,
            )
            values = original.model_dump(mode="python")
            values["record_id"] = source_ref.record_id
            values["provenance"]["provider_record_id"] = source_ref.record_id
            values["provenance"]["provider_series_id"] = "series-123"
            record = type(original).model_validate(values)
            store.seed_records(scope, source_ref.item_id, (record,))
    parameter = scope.requested_parameters[0]
    unit = capabilities.unit_definitions[
        capabilities.get(parameter).unit_policy_id
    ].symbol
    discharge = scope.supporting_parameters[0]
    registry = NumericMethodRegistry(
        (
            *create_production_numeric_method_registry(capabilities).methods,
            NumericMethod(
                method_id="test_discharge_from_temporal",
                version=1,
                output_parameter=discharge,
                output_unit="ft3/s",
                inputs=(
                    NumericMethodInput(
                        parameter=parameter,
                        unit=unit,
                        required_payload_type="temporal_estimate",
                    ),
                ),
                calculate=lambda values, _windows, _target: values[0],
                confidence_cap=0.6,
            ),
        )
    )
    plan = await NumericDependencyPlanner(
        registry=registry, reader=store, capability_registry=capabilities
    ).plan(scope, refs)
    expected_missing = len(scope.time_windows) - 2
    assert len(plan.tasks) == 2 * expected_missing
    assert plan.source_fragment_refs == refs
    temporal_tasks = {
        task.task_id: task
        for task in plan.tasks
        if task.method_id.startswith("temporal_gap_fill_")
    }
    numeric_tasks = tuple(
        task for task in plan.tasks if task.method_id == "test_discharge_from_temporal"
    )
    assert len(temporal_tasks) == len(numeric_tasks) == expected_missing
    assert all(
        task.inputs[0].dependency_task_id in temporal_tasks for task in numeric_tasks
    )
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(3),
        capability_registry=capabilities,
    ).run(scope, plan)
    assert not outcome.failed_task_ids and not outcome.blocked_task_ids
    produced = tuple(
        await asyncio.gather(
            *(
                store.read_fragment(scope, reference)
                for reference in outcome.fragment_refs
            )
        )
    )
    assert (
        sum(isinstance(item.payload, TemporalEstimatePayload) for item in produced)
        == expected_missing
    )
    assert (
        sum(isinstance(item.payload, NumericDerivedPayload) for item in produced)
        == expected_missing
    )
    all_refs = (*refs, *outcome.fragment_refs)
    builder = EvidenceBundleBuilder(
        reader=store, writer=store, numeric_registry=registry
    )
    await builder.build(
        scope,
        fragment_refs=all_refs,
        tool_result_refs=(),
        created_at=FixedClock().now(),
    )
    far_temporal = max(
        (
            item
            for item in produced
            if isinstance(item.payload, TemporalEstimatePayload)
        ),
        key=lambda item: max(item.payload.anchor_distance_seconds),
    )
    far_ref = next(
        reference
        for reference in outcome.fragment_refs
        if reference.evidence_id == far_temporal.evidence_id
    )
    far_numeric_ref = next(
        reference
        for reference in outcome.fragment_refs
        if isinstance(
            store.fragments[(reference.item_id, reference.evidence_id)].payload,
            NumericDerivedPayload,
        )
        and far_ref
        in store.fragments[
            (reference.item_id, reference.evidence_id)
        ].payload.input_evidence_refs
    )
    far_key = (far_numeric_ref.item_id, far_numeric_ref.evidence_id)
    far_original = store.fragments[far_key]
    assert far_original.payload.confidence_cap < 0.6
    store.fragments[far_key] = far_original.model_copy(
        update={
            "payload": far_original.payload.model_copy(update={"confidence_cap": 0.5})
        }
    )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            scope,
            fragment_refs=all_refs,
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )
    store.fragments[far_key] = far_original
    temporal_ref = next(
        reference
        for reference in outcome.fragment_refs
        if isinstance(
            store.fragments[(reference.item_id, reference.evidence_id)].payload,
            TemporalEstimatePayload,
        )
    )
    key = (temporal_ref.item_id, temporal_ref.evidence_id)
    original = store.fragments[key]
    for changed in (
        {"plan_digest": "0" * 64},
        {"plan_item_ref": type(outcome.plan_ref)(item_id=str(uuid4()))},
    ):
        store.fragments[key] = original.model_copy(
            update={"payload": original.payload.model_copy(update=changed)}
        )
        with pytest.raises(EvidenceScopeViolationError):
            await builder.build(
                scope,
                fragment_refs=all_refs,
                tool_result_refs=(),
                created_at=FixedClock().now(),
            )
        store.fragments[key] = original


def test_old_temporal_json_without_plan_fields_roundtrips_registry(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    capabilities, refs, fragments = _evidence(scope)
    candidate = select_temporal_candidates(scope, refs, fragments, capabilities)[0]
    _, fragment = build_temporal_fragment(
        scope, candidate, registry_digest=capabilities.manifest_digest
    )
    fragment = fragment.model_copy(
        update={
            "payload": fragment.payload.model_copy(
                update={
                    "input_evidence_refs": tuple(
                        EvidenceFragmentReference(
                            item_id=str(uuid4()), evidence_id=reference.evidence_id
                        )
                        for reference in fragment.payload.input_evidence_refs
                    )
                }
            )
        }
    )
    historical = fragment.model_dump(mode="json", exclude_none=True)
    assert "plan_item_ref" not in historical["payload"]
    registry = create_production_registry()
    adapter = registry._registrations[("evidence_fragment", 1)].adapter
    safe_body = json.dumps(historical, sort_keys=True, separators=(",", ":"))
    item = StoredTranscriptItem(
        item_id=uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        transcript_sequence=1,
        created_at=datetime(2026, 10, 2, tzinfo=UTC),
        content_type="evidence_fragment",
        content_schema_version=1,
        inline_body=safe_body,
        content_bytes=len(safe_body.encode("utf-8")),
    )
    parsed_item = registry.parse(item, safe_body)
    assert parsed_item.parse_status.value == "ok"
    parsed = parsed_item.body
    assert parsed.payload.plan_item_ref is None
    assert parsed.payload.value == fragment.payload.value
    encoded = registry.encode("evidence_fragment", 1, parsed)
    reread = adapter.validate_json(encoded.safe_body)
    assert reread == parsed


def test_water_collection_request_skips_computed_intermediate(scope_factory):
    capability_registry = load_parameter_capability_registry()
    water_input = _key("00010")
    scope = with_intermediate_parameters(scope_factory(), (C, water_input))
    service = object.__new__(EvidenceAcquisitionService)
    service._capability_registry = capability_registry
    request = service._request(scope, "collect_water_observations")
    assert request.parameter_keys == (
        *scope.requested_parameters,
        *scope.supporting_parameters,
        water_input,
    )
    assert C not in request.parameter_keys


def test_production_cross_parameter_method_is_planned(scope_factory):
    asyncio.run(_production_cross_parameter_method_is_planned(scope_factory))


async def _production_cross_parameter_method_is_planned(scope_factory):
    scope = _scope(scope_factory)
    store = MemoryEvidenceStore()
    source = _seed(store, scope, SOURCE_A, "3")
    method = replace(
        _method(
            "production_t", T, (SOURCE_A,), lambda values, _windows, _target: values[0]
        ),
        production=True,
    )
    registry = NumericMethodRegistry((method,))
    plan = await NumericDependencyPlanner(registry=registry, reader=store).plan(
        scope, (source,)
    )
    assert len(plan.tasks) == 1
    assert plan.tasks[0].method_id == method.method_id
    assert plan.tasks[0].inputs[0].evidence_ref == source


def test_prepare_scope_closes_production_cross_window_dependencies(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 1, tzinfo=UTC))
    )
    target = scope.requested_parameters[0]
    registry = NumericMethodRegistry(
        (
            NumericMethod(
                method_id="test_production_cross_window_target",
                version=1,
                output_parameter=target,
                output_unit="unit",
                inputs=(
                    NumericMethodInput(
                        parameter=A, unit="unit", window_relation="nearest_before"
                    ),
                ),
                calculate=lambda values, _windows, _target: values[0],
                confidence_cap=0.6,
                production=True,
            ),
            replace(
                _method(
                    "production_cross_window_intermediate",
                    A,
                    (B,),
                    lambda values, _windows, _target: values[0],
                ),
                production=True,
            ),
        )
    )
    prepared = prepare_numeric_scope(scope, registry)
    assert set(prepared.intermediate_parameters) == {A, B}


def test_unselected_temporal_candidates_are_not_planned(scope_factory):
    asyncio.run(_unselected_temporal_candidates_are_not_planned(scope_factory))


async def _unselected_temporal_candidates_are_not_planned(scope_factory):
    source_scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    capabilities, references, fragments = _evidence(source_scope)
    assert select_temporal_candidates(source_scope, references, fragments, capabilities)
    scope = SimpleNamespace(
        run_id=source_scope.run_id,
        session_id=source_scope.session_id,
        agent_comid=source_scope.agent_comid,
        target_node=source_scope.target_node,
        time_windows=source_scope.time_windows,
        requested_parameters=(U,),
        supporting_parameters=(),
        intermediate_parameters=source_scope.requested_parameters,
        cancel_token=source_scope.cancel_token,
    )
    store = MemoryEvidenceStore()
    for reference, fragment in zip(references, fragments, strict=True):
        store.seed_fragment(reference, fragment)
        store.seed_scope(
            reference.item_id, run_id=scope.run_id, session_id=scope.session_id
        )
    direct_target = _seed(store, scope, U, "3")
    plan = await NumericDependencyPlanner(
        registry=create_production_numeric_method_registry(capabilities),
        reader=store,
        capability_registry=capabilities,
    ).plan(scope, (*references, direct_target))
    assert plan.tasks == ()


def test_temporal_task_identity_includes_provider_source(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    capabilities, refs, fragments = _evidence(scope)
    candidate = select_temporal_candidates(scope, refs, fragments, capabilities)[0]
    registry = create_production_numeric_method_registry(capabilities)
    original = _temporal_task(scope, registry, candidate)
    alias = _temporal_task(
        scope, registry, replace(candidate, source="wdfn_time_series")
    )
    assert original is not None and alias is not None
    assert original.task_id != alias.task_id


def test_planned_temporal_retry_uses_new_identity_when_plan_changes(scope_factory):
    asyncio.run(_planned_temporal_retry_uses_new_identity(scope_factory))


async def _planned_temporal_retry_uses_new_identity(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    capabilities, references, fragments = _evidence(scope)

    class IdempotentStore(MemoryEvidenceStore):
        def __init__(self):
            super().__init__()
            self.by_operation = {}
            self.reused_operations = 0

        async def write_fragment(self, scope, *, operation_id, fragment):
            if operation_id in self.by_operation:
                self.reused_operations += 1
                return self.by_operation[operation_id]
            reference = await super().write_fragment(
                scope, operation_id=operation_id, fragment=fragment
            )
            self.by_operation[operation_id] = reference
            return reference

    store = IdempotentStore()
    for reference, fragment in zip(references, fragments, strict=True):
        store.seed_fragment(reference, fragment)
        store.seed_scope(
            reference.item_id, run_id=scope.run_id, session_id=scope.session_id
        )
    unrelated = _seed(store, scope, SOURCE_A, "1")
    registry = create_production_numeric_method_registry(capabilities)
    planner = NumericDependencyPlanner(
        registry=registry, reader=store, capability_registry=capabilities
    )
    executor = NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(2),
        capability_registry=capabilities,
    )
    first_plan = await planner.plan(scope, references)
    second_plan = await planner.plan(scope, (*references, unrelated))
    assert first_plan.plan_digest != second_plan.plan_digest
    assert tuple(task.task_id for task in first_plan.tasks) == tuple(
        task.task_id for task in second_plan.tasks
    )
    first = await executor.run(scope, first_plan)
    second = await executor.run(scope, second_plan)
    assert first.fragment_refs and len(first.fragment_refs) == len(second.fragment_refs)
    assert not first.failed_task_ids and not second.failed_task_ids
    assert store.reused_operations == 0
    for old_ref, new_ref in zip(first.fragment_refs, second.fragment_refs, strict=True):
        old = await store.read_fragment(scope, old_ref)
        new = await store.read_fragment(scope, new_ref)
        assert old.evidence_id != new.evidence_id
        assert old.provenance.operation_id != new.provenance.operation_id


def test_shared_model_range_is_saved_once_and_blocked_formula_can_fall_back(scope_factory):
    async def exercise():
        scope = _scope(scope_factory, targets=(T, U))
        scope.intermediate_parameters = (A,)
        store = MemoryEvidenceStore()
        registry = NumericMethodRegistry((
            _method("formula_t", T, (A,), lambda values, *_: values[0] * 2),
            _method("formula_u", U, (A,), lambda values, *_: values[0] * 3),
            *(NumericMethod(
                method_id=f"estimate_{parameter.code}", version=1,
                output_parameter=parameter, output_unit="unit", inputs=(),
                calculate=None, confidence_cap=1, kind="model_estimate",
            ) for parameter in (A, T, U)),
        ))
        planner = NumericDependencyPlanner(registry=registry, reader=store)
        executor = NumericDependencyExecutor(
            registry=registry, reader=store, writer=store, limiter=asyncio.Semaphore(2),
        )
        called = []

        async def estimate(request):
            called.append(request.task.output_parameter)
            if request.task.output_parameter != A:
                assert len(request.input_fragments) == 1
                saved = await store.read_fragment(scope, request.input_refs[0])
                assert saved == request.input_fragments[0]
                assert saved.parameter_refs == (A,)
                assert saved.payload.value is None
            return NumericEstimateResult(
                value=None, value_lower="1", value_upper="3", confidence=0.2,
                rationale="Range based on available related context.",
                model_provider="test", model_name="test", operation_id="estimate-call",
                prompt_digest="test-prompt",
            )

        first_plan = await planner.plan(scope, (), allow_estimation=True)
        assert len(first_plan.tasks) == 3
        first = await executor.run(scope, first_plan, estimator=estimate)
        assert called == [A]
        assert len(first.blocked_task_ids) == 2 and not first.failed_task_ids
        fallback = await planner.plan(
            scope, first.fragment_refs, allow_estimation=True,
            excluded_task_ids=first.blocked_task_ids,
        )
        assert len(fallback.tasks) == 2
        second = await executor.run(scope, fallback, estimator=estimate)
        assert not second.blocked_task_ids and not second.failed_task_ids
        assert sorted(parameter.code for parameter in called) == sorted(parameter.code for parameter in (A, T, U))
        refs = (*first.fragment_refs, *second.fragment_refs)
        assert (await planner.plan(scope, refs, allow_estimation=True)).tasks == ()

    asyncio.run(exercise())


def test_estimate_keeps_original_anchor_beyond_prompt_context_limit(scope_factory):
    async def exercise():
        scope = _scope(scope_factory)
        store = MemoryEvidenceStore()
        anchor_ref = _seed(store, scope, T, "7.2")
        anchor_fragment = await store.read_fragment(scope, anchor_ref)
        context_refs = []
        for index in range(50):
            fragment = EvidenceFragment.model_validate({
                **anchor_fragment.model_dump(mode="python"),
                "evidence_id": f"context-{index:03d}",
                "parameter_refs": (U,) if index == 49 else (T,),
                "data_type": "context",
                "applicability": EvidenceApplicability.CONTEXTUAL_ONLY,
                "series_identity": None,
                "payload": ContextPayload(
                    summary_fields=(("related_reading", str(index)),),
                    record_refs=(),
                ),
                "source_record_refs": (),
                "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
                "quality_class": (
                    EvidenceQualityClass.SUSPICIOUS if index == 48
                    else anchor_fragment.quality_class
                ),
                "quality_flags": (
                    (DataQualityFlag.PROVIDER_FLAGGED,) if index == 48 else ()
                ),
            })
            reference = EvidenceFragmentReference(
                item_id=str(uuid4()), evidence_id=fragment.evidence_id
            )
            store.seed_fragment(reference, fragment)
            store.seed_scope(
                reference.item_id, run_id=scope.run_id,
                session_id=scope.session_id,
            )
            context_refs.append(reference)

        registry = NumericMethodRegistry((NumericMethod(
            method_id="estimate_target", version=1,
            output_parameter=T, output_unit="unit", inputs=(),
            calculate=None, confidence_cap=1, kind="model_estimate",
        ),))
        task = NumericPlanTask(
            task_id="estimate-target", target_comid=scope.agent_comid,
            output_parameter=T, output_unit="unit",
            window_id=scope.time_windows[0].window_id,
            mode=scope.mode.value if hasattr(scope, "mode") else "gap_filling",
            method_id="estimate_target", method_version=1,
            method_kind="model_estimate", inputs=(),
        )
        plan = NumericDependencyPlan.create(
            run_id=scope.run_id, session_id=scope.session_id,
            target_comid=scope.agent_comid,
            method_registry_digest=registry.manifest_digest,
            source_fragment_refs=(*context_refs, anchor_ref),
            tasks=(task,),
        )
        received = []

        async def estimate(request):
            received.append(request)
            return NumericEstimateResult(
                value="7.2", value_lower="6.8", value_upper="7.6",
                confidence=0.4, rationale="An original pH reading is available.",
                model_provider="test", model_name="test",
                operation_id="estimate-call", prompt_digest="prompt",
            )

        outcome = await NumericDependencyExecutor(
            registry=registry, reader=store, writer=store,
            limiter=asyncio.Semaphore(2),
        ).run(scope, plan, estimator=estimate)
        assert not outcome.failed_task_ids
        assert len(received) == 1
        assert len(received[0].context_refs) == 48
        assert anchor_ref in received[0].context_refs
        assert context_refs[-1] in received[0].context_refs
        assert context_refs[48] not in received[0].context_refs
        assert received[0].measured_anchors
        saved = await store.read_fragment(scope, outcome.fragment_refs[0])
        assert saved.payload.measured_anchors
        assert DataQualityFlag.PROVIDER_FLAGGED not in saved.quality_flags

    asyncio.run(exercise())


@pytest.mark.parametrize("code", ("00400", "00403"))
def test_full_ph_scale_is_not_saved_as_a_prediction(scope_factory, code):
    async def exercise():
        parameter = _key(code)
        scope = _scope(scope_factory, targets=(parameter,))
        store = MemoryEvidenceStore()
        registry = NumericMethodRegistry((NumericMethod(
            method_id="estimate_ph", version=1, output_parameter=parameter,
            output_unit="standard units", inputs=(), calculate=None,
            confidence_cap=1, kind="model_estimate",
        ),))
        task = NumericPlanTask(
            task_id="estimate-ph", target_comid=scope.agent_comid,
            output_parameter=parameter, output_unit="standard units",
            window_id=scope.time_windows[0].window_id, mode="gap_filling",
            method_id="estimate_ph", method_version=1,
            method_kind="model_estimate", inputs=(),
        )
        plan = NumericDependencyPlan.create(
            run_id=scope.run_id, session_id=scope.session_id,
            target_comid=scope.agent_comid,
            method_registry_digest=registry.manifest_digest, tasks=(task,),
        )

        async def estimate(_request):
            return NumericEstimateResult(
                value=None, value_lower="0", value_upper="14", confidence=0.1,
                rationale="Only the full pH scale is available.",
                model_provider="test", model_name="test",
                operation_id="estimate-call", prompt_digest="prompt",
            )

        outcome = await NumericDependencyExecutor(
            registry=registry, reader=store, writer=store,
            limiter=asyncio.Semaphore(1),
        ).run(scope, plan, estimator=estimate)
        assert outcome.fragment_refs == ()
        assert outcome.failed_task_ids == (task.task_id,)

    asyncio.run(exercise())
