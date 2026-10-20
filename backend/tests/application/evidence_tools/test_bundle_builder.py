import asyncio
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import pytest
from pydantic import ValidationError

from app.application.evidence_tools import (
    EvidenceBundleBuilder,
    EvidenceCollectionArtifacts,
    EvidenceCollectionInput,
    EvidenceCollectionService,
    EvidenceScopeViolationError,
    with_intermediate_parameters,
)
from app.application.evidence_tools.bundle_builder import _compact_covered_observations
from app.application.evidence_tools.numeric_dependency import (
    NumericDependencyExecutor,
    NumericDependencyPlanner,
)
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
    create_production_numeric_method_registry,
)
from app.domain import (
    AggregatePayload,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    ConversionPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceAggregationMethod,
    EvidenceApplicability,
    EvidenceBundle,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ItemReference,
    NumericDerivedPayload,
    ParameterProvider,
    ProviderCollectionBatch,
    SourceRecordReference,
    TemporalEstimatePayload,
    TimeRange,
    canonical_model_bytes,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import make_context
from tests.application.evidence_tools.fakes import FixedClock, MemoryEvidenceStore
from tests.application.evidence_tools.test_collection_service import observation


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def provider_batch(
    scope, records, *, complete=True, completed_ranges=None, operation_id="provider-bundle"
):
    if completed_ranges is None:
        completed_ranges = (
            TimeRange(
                start=scope.time_windows[0].time_range.start,
                end=scope.time_windows[-1].time_range.end,
            ),
        ) if complete else ()
    page_id = f"page:{operation_id}"
    return ProviderCollectionBatch(
        operation_id=operation_id,
        records=tuple(records),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity=page_id,
                record_count=len(records),
                response_bytes=512,
                page_complete=complete,
            ),
        ),
        completed_ranges=completed_ranges,
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=512,
        ),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=complete,
            terminal_identity=page_id if complete else None,
            digest=f"digest:{complete}:{len(records)}",
        ),
        incomplete_reason=None if complete else "incomplete_source",
    )


def collection(scope, provider_batch, *, parameter_keys=None, requested_series=None):
    return EvidenceCollectionInput(
        provider=ParameterProvider.WDFN_TIME_SERIES.value,
        channel="continuous",
        request_range=scope.query_envelope,
        requested_series=requested_series or ("series-do", "series-flow"),
        parameter_keys=parameter_keys or (
            scope.requested_parameters[0],
            scope.supporting_parameters[0],
        ),
        affected_window_ids=tuple(item.window_id for item in scope.time_windows),
        batch=provider_batch,
        station_id="USGS-123",
    )


async def assembled(scope, *, complete=True, completed_ranges=None):
    records = tuple(
        record
        for index, _ in enumerate(scope.time_windows)
        for record in (
            observation(scope, index * 2, window_index=index),
            observation(
                scope,
                index * 2 + 1,
                parameter=scope.supporting_parameters[0],
                window_index=index,
            ),
        )
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )
    artifacts = await service.process(
        scope,
        operation_id="bundle-collection",
        collections=(
            collection(
                scope,
                provider_batch(
                    scope,
                    records,
                    complete=complete,
                    completed_ranges=completed_ranges,
                ),
            ),
        ),
    )
    store.seed_scope("tool-result-1")
    return store, artifacts


async def many_observations(scope, *, count=96, concentrated=False, provisional=False):
    window = scope.time_windows[0].time_range
    window_seconds = int((window.end - window.start).total_seconds())
    by_parameter = tuple(
        tuple(
            observation(
                scope,
                index + (count if is_support else 0),
                parameter=(
                    scope.supporting_parameters[0]
                    if is_support
                    else scope.requested_parameters[0]
                ),
                approval=(
                    "provisional"
                    if provisional and not is_support and index == 0
                    else "approved"
                ),
            ).model_copy(update={
                "value": "35" if is_support else "7",
                "observed_time": window.start + timedelta(
                    seconds=index * 60 if concentrated else index * window_seconds // count
                ),
            })
            for index in range(count)
        )
        for is_support in (False, True)
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )
    target = await service.process(
        scope,
        operation_id="many-target",
        collections=(
            collection(
                scope,
                provider_batch(scope, by_parameter[0], operation_id="provider-target"),
                parameter_keys=(scope.requested_parameters[0],),
                requested_series=("series-do",),
            ),
        ),
    )
    support = await service.process(
        scope,
        operation_id="many-support",
        collections=(
            collection(
                scope,
                provider_batch(scope, by_parameter[1], operation_id="provider-support"),
                parameter_keys=(scope.supporting_parameters[0],),
                requested_series=("series-flow",),
            ),
        ),
    )
    artifacts = EvidenceCollectionArtifacts(
        manifest_refs=(*target.manifest_refs, *support.manifest_refs),
        source_record_refs=(*target.source_record_refs, *support.source_record_refs),
        fragment_refs=(*target.fragment_refs, *support.fragment_refs),
        collection_fact_refs=(*target.collection_fact_refs, *support.collection_fact_refs),
    )
    store.seed_scope("tool-result-1")
    return store, artifacts


async def add_mean(
    scope, store, raw_refs, parameter, *, input_refs=None, invalid=0, omit_raw_flags=False
):
    raw = tuple(
        store.fragments[(reference.item_id, reference.evidence_id)]
        for reference in raw_refs
        if (
            fragment := store.fragments[(reference.item_id, reference.evidence_id)]
        ).parameter_refs == (parameter,)
        and fragment.time_range == scope.time_windows[0].time_range
        and isinstance(fragment.payload, DirectRecordPayload)
    )
    assert raw
    first = raw[0]
    source_refs = input_refs or tuple(item.payload.record_ref for item in raw)
    aggregate = EvidenceFragment(
        evidence_id=f"preflight-mean:{parameter.code}",
        source="deterministic_aggregation",
        collection_status="available",
        target_comid=scope.agent_comid,
        location=EvidenceLocation(
            point=None,
            comid=scope.agent_comid,
            station_id=first.location.station_id,
            distance_to_target_km=None,
        ),
        time_range=scope.time_windows[0].time_range,
        parameter_refs=(parameter,),
        data_type="aggregate",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity=first.series_identity,
        quality_class=EvidenceQualityClass.SUSPICIOUS,
        quality_flags=tuple(sorted(
            {
                DataQualityFlag.PARTIAL_COVERAGE,
                *(
                    flag
                    for item in raw
                    for flag in item.quality_flags
                    if not omit_raw_flags
                ),
            },
            key=lambda flag: flag.value,
        )),
        coverage=EvidenceCoverage(
            status="partial",
            observed_count=len(source_refs),
            covered_duration_seconds=0,
            window_duration_seconds=int(
                (first.time_range.end - first.time_range.start).total_seconds()
            ),
        ),
        payload=AggregatePayload(
            value="7" if parameter == scope.requested_parameters[0] else "35",
            unit=first.payload.displayed_unit,
            method=EvidenceAggregationMethod.MEAN,
            input_record_refs=source_refs,
            valid_input_count=len(source_refs) - invalid - sum(
                item.quality_class is EvidenceQualityClass.SUSPICIOUS
                for item in raw
                if item.payload.record_ref in source_refs
            ),
            suspicious_input_count=sum(
                item.quality_class is EvidenceQualityClass.SUSPICIOUS
                for item in raw
                if item.payload.record_ref in source_refs
            ),
            invalid_input_count=invalid,
        ),
        source_record_refs=source_refs,
        derivation=EvidenceDerivation(
            algorithm="decimal_mean",
            algorithm_version=1,
            policy_id="continuous_observation",
            registry_digest="sha256:registry",
        ),
        provenance={"operation_id": "preflight-mean"},
        result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
    )
    return await store.write_fragment(scope, operation_id="preflight-mean", fragment=aggregate)


@run_async
async def test_bundle_has_exact_target_and_discharge_cells_with_reference_closure(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    built = await builder.build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )

    bundle = built.bundle
    assert built.validated_inputs.fragments
    assert len(bundle.cells) == len(evidence_scope.time_windows) * 2
    assert {cell.role for cell in bundle.cells} == {
        EvidenceCellRole.TARGET,
        EvidenceCellRole.REQUIRED_SUPPORT,
    }
    assert {cell.status for cell in bundle.cells} == {EvidenceCellStatus.SUPPORTED}
    assert bundle.source_record_item_refs
    assert not hasattr(bundle, "fragments")


@run_async
async def test_method_input_cells_are_separate_from_target_and_support(evidence_scope):
    parameter = evidence_scope.requested_parameters[0].model_copy(update={
        "code": "00010",
    })
    scoped = with_intermediate_parameters(evidence_scope, (parameter,))
    store, artifacts = await assembled(scoped)
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        scoped,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(),
        created_at=FixedClock().now(),
    )

    assert built.bundle.intermediate_parameters == (parameter,)
    assert len(built.bundle.cells) == len(scoped.time_windows) * 3
    assert all(
        cell.status is EvidenceCellStatus.UNSUPPORTED
        for cell in built.bundle.cells
        if cell.role is EvidenceCellRole.METHOD_INPUT
    )
    assert all(
        cell.status is EvidenceCellStatus.SUPPORTED
        for cell in built.bundle.cells
        if cell.role is not EvidenceCellRole.METHOD_INPUT
    )


@run_async
async def test_complete_means_compact_96_target_and_96_support_records(evidence_scope):
    store, artifacts = await many_observations(evidence_scope)
    raw_refs = artifacts.fragment_refs
    assert len(raw_refs) == 192
    target_mean = await add_mean(
        evidence_scope, store, raw_refs, evidence_scope.requested_parameters[0]
    )
    support_mean = await add_mean(
        evidence_scope, store, raw_refs, evidence_scope.supporting_parameters[0]
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(
            *raw_refs,
            *artifacts.collection_fact_refs,
            target_mean,
            support_mean,
        ),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )

    cells = {
        (cell.window_id, cell.role): cell for cell in built.bundle.cells
    }
    first_window = evidence_scope.time_windows[0].window_id
    assert cells[(first_window, EvidenceCellRole.TARGET)].direct_fragment_refs == (
        target_mean.evidence_id,
    )
    assert cells[(first_window, EvidenceCellRole.REQUIRED_SUPPORT)].direct_fragment_refs == (
        support_mean.evidence_id,
    )
    assert sum(
        len(cells[(first_window, role)].direct_fragment_refs)
        for role in (EvidenceCellRole.TARGET, EvidenceCellRole.REQUIRED_SUPPORT)
    ) == 2
    assert not {reference.evidence_id for reference in raw_refs} & {
        reference.evidence_id for reference in built.bundle.fragment_refs
    }
    assert not {reference.evidence_id for reference in raw_refs} & {
        reference.evidence_id for reference, _ in built.validated_inputs.fragments
    }
    assert len(built.validated_inputs.source_records) == 2
    assert tuple(
        len(records) for _, records in built.validated_inputs.source_records
    ) == (96, 96)
    assert len(
        store.fragments[(target_mean.item_id, target_mean.evidence_id)]
        .payload.input_record_refs
    ) == 96
    assert len(
        store.fragments[(support_mean.item_id, support_mean.evidence_id)]
        .payload.input_record_refs
    ) == 96
    assert all((item.item_id, item.evidence_id) in store.fragments for item in raw_refs)
    EvidenceBundle.model_validate(built.bundle.model_dump(mode="python"))


@run_async
async def test_mean_compaction_keeps_raw_fragment_used_by_conversion(evidence_scope):
    store, artifacts = await many_observations(evidence_scope)
    raw_refs = tuple(
        reference
        for reference in artifacts.fragment_refs
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (evidence_scope.requested_parameters[0],)
    )
    mean_ref = await add_mean(
        evidence_scope,
        store,
        artifacts.fragment_refs,
        evidence_scope.requested_parameters[0],
    )
    first = store.fragments[(raw_refs[0].item_id, raw_refs[0].evidence_id)]
    conversion = EvidenceFragment(
        evidence_id="unit-conversion-of-raw",
        source="deterministic_conversion",
        collection_status="available",
        target_comid=evidence_scope.agent_comid,
        location=first.location,
        time_range=first.time_range,
        parameter_refs=first.parameter_refs,
        data_type="conversion",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity=first.series_identity,
        quality_class=first.quality_class,
        quality_flags=first.quality_flags,
        coverage=first.coverage,
        payload=ConversionPayload(
            value=first.payload.displayed_value,
            unit=first.payload.displayed_unit,
            input_evidence_ref=raw_refs[0],
            conversion_id="unit:identity:v1",
        ),
        source_record_refs=(),
        derivation=EvidenceDerivation(
            algorithm="decimal_unit_conversion",
            algorithm_version=1,
            policy_id="unit:identity:v1",
            registry_digest="sha256:registry",
        ),
        provenance={"operation_id": "conversion"},
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
    )
    conversion_ref = await store.write_fragment(
        evidence_scope, operation_id="conversion", fragment=conversion
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(
            *artifacts.fragment_refs,
            *artifacts.collection_fact_refs,
            mean_ref,
            conversion_ref,
        ),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    surviving = {reference.evidence_id for reference in built.bundle.fragment_refs}
    assert raw_refs[0].evidence_id in surviving
    assert not {reference.evidence_id for reference in raw_refs[1:]} & surviving
    assert mean_ref.evidence_id in surviving
    assert conversion_ref.evidence_id in surviving


@run_async
async def test_mean_compaction_keeps_raw_fragment_used_by_numeric_derivation(
    evidence_scope,
):
    store, artifacts = await many_observations(evidence_scope, count=3)
    raw_refs = tuple(
        reference
        for reference in artifacts.fragment_refs
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (evidence_scope.requested_parameters[0],)
    )
    mean_ref = await add_mean(
        evidence_scope,
        store,
        artifacts.fragment_refs,
        evidence_scope.requested_parameters[0],
    )
    mean = store.fragments[(mean_ref.item_id, mean_ref.evidence_id)]
    method_id = "test_numeric_from_raw"
    numeric = EvidenceFragment.model_validate(mean.model_copy(update={
        "evidence_id": "numeric-from-raw",
        "source": "numeric_method",
        "data_type": "numeric_derived",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "relation_id": method_id,
        "quality_flags": (*mean.quality_flags, DataQualityFlag.ESTIMATED),
        "coverage": mean.coverage.model_copy(update={
            "status": CoverageStatus.INSUFFICIENT,
            "observed_count": 0,
        }),
        "payload": NumericDerivedPayload(
            value="7",
            unit=mean.payload.unit,
            method_id=method_id,
            method_version=1,
            task_id="numeric-task-1",
            plan_digest="numeric-plan-digest",
            plan_item_ref=ItemReference(item_id="numeric-plan-1"),
            method_registry_digest="numeric-method-registry",
            input_evidence_refs=(raw_refs[0],),
            input_window_ids=(evidence_scope.time_windows[0].window_id,),
            confidence_cap=0.6,
        ),
        "source_record_refs": (),
        "derivation": EvidenceDerivation(
            algorithm=method_id,
            algorithm_version=1,
            policy_id=method_id,
            registry_digest="numeric-method-registry",
        ),
        "provenance": mean.provenance.model_copy(update={
            "operation_id": "numeric-from-raw",
        }),
    }).model_dump())
    numeric_ref = await store.write_fragment(
        evidence_scope, operation_id="numeric-from-raw", fragment=numeric
    )
    references = (*artifacts.fragment_refs, mean_ref, numeric_ref)
    by_evidence_id = {
        reference.evidence_id: (
            reference,
            store.fragments[(reference.item_id, reference.evidence_id)],
        )
        for reference in references
    }
    records_by_item = {
        item_id: records
        for (run_id, session_id, item_id), records in store.records.items()
        if (run_id, session_id) == (evidence_scope.run_id, evidence_scope.session_id)
    }
    compacted = _compact_covered_observations(
        evidence_scope, by_evidence_id, records_by_item
    )

    assert raw_refs[0].evidence_id in compacted
    assert not {reference.evidence_id for reference in raw_refs[1:]} & compacted.keys()
    assert mean_ref.evidence_id in compacted
    assert numeric_ref.evidence_id in compacted
    assert all(
        reference.evidence_id in compacted
        for reference in numeric.payload.input_evidence_refs
    )


@run_async
@pytest.mark.parametrize("include_numeric", [False, True])
async def test_rebuild_reads_compacted_plan_candidates_without_expanding_bundle(
    scope_factory, include_numeric,
):
    evidence_scope = scope_factory(context=make_context(
        start_date=date(2026, 9, 29),
        end_date=date(2026, 9, 29),
        decision_instant=datetime(2026, 10, 1, tzinfo=timezone.utc),
    ))
    capabilities = load_parameter_capability_registry()
    store, artifacts = await many_observations(evidence_scope, count=3)
    target = evidence_scope.requested_parameters[0]
    support = evidence_scope.supporting_parameters[0]
    target_refs = tuple(
        reference
        for reference in (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (target,)
    )
    raw_refs = tuple(
        reference
        for reference in target_refs
        if isinstance(
            store.fragments[(reference.item_id, reference.evidence_id)].payload,
            DirectRecordPayload,
        )
    )
    mean_ref = await add_mean(
        evidence_scope, store, artifacts.fragment_refs, target
    )
    methods = create_production_numeric_method_registry(capabilities).methods
    if include_numeric:
        target_unit = capabilities.unit_definitions[
            capabilities.get(target).unit_policy_id
        ].symbol
        methods = (*methods, NumericMethod(
            method_id="test_support_from_temporal",
            version=1,
            output_parameter=support,
            output_unit="ft3/s",
            inputs=(NumericMethodInput(
                parameter=target,
                unit=target_unit,
                required_payload_type="temporal_estimate",
            ),),
            calculate=lambda values, _windows, _target: values[0],
            confidence_cap=0.6,
        ))
    registry = NumericMethodRegistry(methods)
    source_refs = (*target_refs, mean_ref)
    plan = await NumericDependencyPlanner(
        registry=registry, reader=store, capability_registry=capabilities
    ).plan(evidence_scope, source_refs)
    assert set(raw_refs) <= set(plan.source_fragment_refs)
    outcome = await NumericDependencyExecutor(
        registry=registry,
        reader=store,
        writer=store,
        limiter=asyncio.Semaphore(2),
        capability_registry=capabilities,
    ).run(evidence_scope, plan)
    assert not outcome.failed_task_ids and not outcome.blocked_task_ids
    produced = tuple(
        store.fragments[(reference.item_id, reference.evidence_id)]
        for reference in outcome.fragment_refs
    )
    assert any(
        isinstance(fragment.payload, TemporalEstimatePayload)
        for fragment in produced
    )
    assert any(
        isinstance(fragment.payload, NumericDerivedPayload)
        for fragment in produced
    ) is include_numeric
    builder = EvidenceBundleBuilder(
        reader=store, writer=store, numeric_registry=registry
    )
    first = await builder.build_and_write(
        evidence_scope,
        fragment_refs=(*source_refs, *outcome.fragment_refs),
        tool_result_refs=(),
        created_at=FixedClock().now(),
    )
    assert not {reference.evidence_id for reference in raw_refs} & {
        reference.evidence_id for reference in first.bundle.fragment_refs
    }
    second = await builder.build(
        evidence_scope,
        fragment_refs=first.bundle.fragment_refs,
        tool_result_refs=first.bundle.tool_result_refs,
        created_at=FixedClock().now(),
    )
    assert second.bundle.fragment_refs == first.bundle.fragment_refs
    if include_numeric:
        numeric = next(
            fragment for fragment in produced
            if isinstance(fragment.payload, NumericDerivedPayload)
        )
        actual_input = numeric.payload.input_evidence_refs[0]
        with pytest.raises(EvidenceScopeViolationError):
            await builder.build(
                evidence_scope,
                fragment_refs=tuple(
                    reference for reference in first.bundle.fragment_refs
                    if reference != actual_input
                ),
                tool_result_refs=(),
                created_at=FixedClock().now(),
            )

    source = raw_refs[0]
    source_key = (source.item_id, source.evidence_id)
    original = store.fragments.pop(source_key)
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=first.bundle.fragment_refs,
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )
    store.fragments[source_key] = original

    store.seed_scope(
        source.item_id,
        run_id=evidence_scope.run_id,
        session_id="other-session",
    )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=first.bundle.fragment_refs,
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )


@run_async
@pytest.mark.parametrize(
    "problem",
    ["partial", "invalid_input", "mixed_series", "out_of_window", "missing_quality_flag"],
)
async def test_incomplete_mean_keeps_raw_record_refs(evidence_scope, problem):
    store, artifacts = await many_observations(
        evidence_scope, provisional=problem == "missing_quality_flag"
    )
    raw_refs = tuple(
        reference
        for reference in artifacts.fragment_refs
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (evidence_scope.requested_parameters[0],)
    )
    source_refs = tuple(
        store.fragments[(reference.item_id, reference.evidence_id)].payload.record_ref
        for reference in raw_refs
    )
    aggregate_ref = await add_mean(
        evidence_scope,
        store,
        artifacts.fragment_refs,
        evidence_scope.requested_parameters[0],
        input_refs=source_refs[:-1] if problem == "partial" else source_refs,
        invalid=1 if problem == "invalid_input" else 0,
        omit_raw_flags=problem == "missing_quality_flag",
    )
    if problem == "missing_quality_flag":
        assert any(
            DataQualityFlag.PROVIDER_FLAGGED in
            store.fragments[(reference.item_id, reference.evidence_id)].quality_flags
            for reference in raw_refs
        )
        aggregate = store.fragments[(aggregate_ref.item_id, aggregate_ref.evidence_id)]
        assert aggregate.payload.suspicious_input_count == 1
        assert DataQualityFlag.PROVIDER_FLAGGED not in aggregate.quality_flags
    if problem == "mixed_series":
        last = raw_refs[-1]
        key = (last.item_id, last.evidence_id)
        store.fragments[key] = store.fragments[key].model_copy(
            update={"series_identity": "another-series"}
        )
    if problem == "out_of_window":
        source_item = source_refs[-1].item_id
        key = (evidence_scope.run_id, evidence_scope.session_id, source_item)
        store.records[key] = tuple(
            record.model_copy(
                update={"observed_time": evidence_scope.time_windows[0].time_range.end}
            ) if record.record_id == source_refs[-1].record_id else record
            for record in store.records[key]
        )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(
            *artifacts.fragment_refs,
            *artifacts.collection_fact_refs,
            aggregate_ref,
        ),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    target = next(
        cell
        for cell in built.bundle.cells
        if cell.window_id == evidence_scope.time_windows[0].window_id
        and cell.role is EvidenceCellRole.TARGET
    )
    assert set(target.direct_fragment_refs) == {
        *(reference.evidence_id for reference in raw_refs),
        aggregate_ref.evidence_id,
    }
    assert {reference.evidence_id for reference in raw_refs} <= {
        reference.evidence_id for reference in built.bundle.fragment_refs
    }


@run_async
async def test_complete_short_burst_mean_represents_its_selected_window(evidence_scope):
    store, artifacts = await many_observations(evidence_scope, concentrated=True)
    mean_ref = await add_mean(
        evidence_scope,
        store,
        artifacts.fragment_refs,
        evidence_scope.requested_parameters[0],
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(
            *artifacts.fragment_refs,
            *artifacts.collection_fact_refs,
            mean_ref,
        ),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    target_raw = {
        reference.evidence_id
        for reference in artifacts.fragment_refs
        if store.fragments[(reference.item_id, reference.evidence_id)].parameter_refs
        == (evidence_scope.requested_parameters[0],)
    }
    retained = {reference.evidence_id for reference in built.bundle.fragment_refs}
    assert target_raw.isdisjoint(retained)
    assert mean_ref.evidence_id in retained


@run_async
@pytest.mark.parametrize("method", [EvidenceAggregationMethod.MEAN, EvidenceAggregationMethod.LATEST])
async def test_temporal_estimate_closes_only_nearest_original_window_aggregates(scope_factory, method):
    scope = scope_factory(context=make_context(
        start_date=date(2026, 9, 27),
        end_date=date(2026, 9, 29),
        decision_instant=datetime(2026, 10, 1, tzinfo=timezone.utc),
    ))
    store = MemoryEvidenceStore()
    store.seed_scope("tool-result-1")
    parameter = scope.requested_parameters[0]
    anchors = []
    for index, value in ((0, "7"), (2, "9")):
        window = scope.time_windows[index]
        record = observation(scope, index, window_index=index).model_copy(
            update={
                "value": value,
                **({"provider": ParameterProvider.USGS_SAMPLES.value,
                    "channel": "discrete_samples"}
                   if method is EvidenceAggregationMethod.LATEST else {}),
            }
        )
        item_id = f"source-{index}"
        store.seed_records(scope, item_id, (record,))
        source_ref = SourceRecordReference(item_id=item_id, record_id=record.record_id)
        anchor = EvidenceFragment(
            evidence_id=f"mean-{index}",
            source="deterministic_aggregation",
            collection_status="available",
            target_comid=scope.agent_comid,
            location=EvidenceLocation(
                point=None,
                comid=scope.agent_comid,
                station_id="USGS-123",
                distance_to_target_km=None,
            ),
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="aggregate",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity="series-123-00300",
            quality_class=EvidenceQualityClass.SUSPICIOUS,
            quality_flags=(DataQualityFlag.PARTIAL_COVERAGE,),
            coverage=EvidenceCoverage(
                status="partial",
                observed_count=1,
                covered_duration_seconds=0,
                window_duration_seconds=21600,
            ),
            payload=AggregatePayload(
                value=value,
                unit=record.raw_unit,
                method=method,
                input_record_refs=(source_ref,),
                valid_input_count=1,
                suspicious_input_count=0,
                invalid_input_count=0,
            ),
            source_record_refs=(source_ref,),
            derivation=EvidenceDerivation(
                algorithm=f"decimal_{method.value}",
                algorithm_version=1,
                policy_id="continuous_observation",
                registry_digest="sha256:registry",
            ),
            provenance={"operation_id": f"mean-{index}"},
            result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
        )
        anchors.append(await store.write_fragment(
            scope, operation_id=f"mean-{index}", fragment=anchor
        ))

    estimate = EvidenceFragment(
        evidence_id="temporal-estimate-1",
        source="m5_temporal_gap_fill",
        collection_status="available",
        target_comid=scope.agent_comid,
        location=EvidenceLocation(
            point=None,
            comid=scope.agent_comid,
            station_id="USGS-123",
            distance_to_target_km=None,
        ),
        time_range=scope.time_windows[1].time_range,
        parameter_refs=(parameter,),
        data_type="temporal_estimate",
        applicability=EvidenceApplicability.ALLOWED_INDIRECT,
        relation_id="temporal_gap_fill_v1",
        series_identity="series-123-00300",
        quality_class=EvidenceQualityClass.SUSPICIOUS,
        quality_flags=(DataQualityFlag.TEMPORAL_ESTIMATE, DataQualityFlag.PARTIAL_COVERAGE),
        coverage=EvidenceCoverage(
            status="insufficient",
            observed_count=0,
            covered_duration_seconds=0,
            window_duration_seconds=21600,
        ),
        payload=TemporalEstimatePayload(
            value="8",
            unit=record.raw_unit,
            method="linear_interpolation",
            input_evidence_refs=tuple(anchors),
            anchor_window_ids=(scope.time_windows[0].window_id, scope.time_windows[2].window_id),
            anchor_distance_seconds=(21600, 21600),
        ),
        derivation=EvidenceDerivation(
            algorithm="temporal_gap_fill",
            algorithm_version=1,
            policy_id="temporal_gap_fill_v1",
            registry_digest="sha256:registry",
        ),
        provenance={"operation_id": "temporal-estimate"},
        result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
    )
    estimate_ref = await store.write_fragment(
        scope, operation_id="temporal-estimate", fragment=estimate
    )

    async def build():
        return await EvidenceBundleBuilder(reader=store, writer=store).build(
            scope,
            fragment_refs=(*anchors, estimate_ref),
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )

    built = await build()
    target = next(cell for cell in built.bundle.cells if (
        cell.window_id == scope.time_windows[1].window_id
        and cell.parameter == parameter
    ))
    assert target.allowed_indirect_fragment_refs == (estimate.evidence_id,)
    assert set(built.bundle.source_record_item_refs) == {"source-0", "source-2"}

    if method is EvidenceAggregationMethod.LATEST:
        key = (scope.run_id, scope.session_id, "source-0")
        original_records = store.records[key]
        store.records[key] = tuple(
            item.model_copy(update={"channel": "continuous"})
            for item in original_records
        )
        with pytest.raises(EvidenceScopeViolationError):
            await build()
        store.records[key] = original_records

    for updates in (
        {"anchor_distance_seconds": (1, 1)},
        {"value": "8.5"},
        {"input_evidence_refs": tuple(reversed(anchors))},
    ):
        key = (estimate_ref.item_id, estimate_ref.evidence_id)
        store.fragments[key] = estimate.model_copy(update={
            "payload": estimate.payload.model_copy(update=updates)
        })
        with pytest.raises(EvidenceScopeViolationError):
            await build()


@run_async
@pytest.mark.parametrize("missing_role", [EvidenceCellRole.TARGET, EvidenceCellRole.REQUIRED_SUPPORT])
async def test_target_and_discharge_missing_states_are_independent(
    evidence_scope,
    missing_role,
):
    store, artifacts = await assembled(evidence_scope)
    parameter = (
        evidence_scope.requested_parameters[0]
        if missing_role is EvidenceCellRole.TARGET
        else evidence_scope.supporting_parameters[0]
    )
    first_window = evidence_scope.time_windows[0]
    refs = tuple(
        reference
        for reference in (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
        if not (
            (fragment := store.fragments[(reference.item_id, reference.evidence_id)])
            .parameter_refs
            == (parameter,)
            and fragment.time_range == first_window.time_range
            and fragment.data_type != "collection_fact"
        )
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=refs,
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    bundle = built.bundle
    cells = {
        (cell.window_id, cell.role): cell
        for cell in bundle.cells
    }
    missing = cells[(first_window.window_id, missing_role)]
    other_role = (
        EvidenceCellRole.REQUIRED_SUPPORT
        if missing_role is EvidenceCellRole.TARGET
        else EvidenceCellRole.TARGET
    )
    assert missing.status is EvidenceCellStatus.UNSUPPORTED
    assert cells[(first_window.window_id, other_role)].status is EvidenceCellStatus.SUPPORTED


@run_async
async def test_incomplete_windows_block_without_erasing_independently_complete_windows(
    evidence_scope,
):
    midpoint = len(evidence_scope.time_windows) // 2
    completed = (
        TimeRange(
            start=evidence_scope.time_windows[0].time_range.start,
            end=evidence_scope.time_windows[midpoint - 1].time_range.end,
        ),
    )
    store, artifacts = await assembled(
        evidence_scope,
        complete=False,
        completed_ranges=completed,
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    bundle = built.bundle
    by_window = {
        window.window_id: {
            cell.status for cell in bundle.cells if cell.window_id == window.window_id
        }
        for window in evidence_scope.time_windows
    }
    assert all(
        by_window[item.window_id] == {EvidenceCellStatus.SUPPORTED}
        for item in evidence_scope.time_windows[:midpoint]
    )
    assert all(
        by_window[item.window_id] == {EvidenceCellStatus.BLOCKED}
        for item in evidence_scope.time_windows[midpoint:]
    )


@run_async
async def test_all_unproven_windows_produce_a_deterministic_all_blocked_bundle(
    evidence_scope,
):
    store, artifacts = await assembled(
        evidence_scope,
        complete=False,
        completed_ranges=(),
    )
    built = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    bundle = built.bundle
    assert {cell.status for cell in bundle.cells} == {EvidenceCellStatus.BLOCKED}
    assert {reason for cell in bundle.cells for reason in cell.reasons} == {
        "incomplete_source"
    }


@run_async
async def test_builder_rejects_missing_duplicate_cross_scope_and_unpersisted_refs(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    refs = (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=(*refs, refs[0]),
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=(EvidenceFragmentReference(item_id="missing", evidence_id="missing"),),
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="unpersisted-result"),),
            created_at=FixedClock().now(),
        )

    fragment_item = refs[0].item_id
    store.seed_scope(fragment_item, run_id="other-run")
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )


@run_async
async def test_conversion_cycle_is_rejected_before_bundle_assembly(evidence_scope):
    store = MemoryEvidenceStore()
    window = evidence_scope.time_windows[0].time_range
    references = (
        EvidenceFragmentReference(item_id="fragment-a", evidence_id="conversion-a"),
        EvidenceFragmentReference(item_id="fragment-b", evidence_id="conversion-b"),
    )
    for reference, dependency in zip(references, reversed(references), strict=True):
        fragment = EvidenceFragment(
            evidence_id=reference.evidence_id,
            source="deterministic_conversion",
            collection_status="available",
            target_comid=evidence_scope.agent_comid,
            location=EvidenceLocation(
                point=evidence_scope.target_node.point,
                comid=evidence_scope.agent_comid,
                station_id=None,
                distance_to_target_km=0,
            ),
            time_range=window,
            parameter_refs=(evidence_scope.requested_parameters[0],),
            data_type="conversion",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity="series-do",
            quality_class="valid",
            quality_flags=(),
            coverage={
                "status": "partial",
                "observed_count": 1,
                "covered_duration_seconds": 0,
                "window_duration_seconds": int((window.end - window.start).total_seconds()),
            },
            payload=ConversionPayload(
                value="8",
                unit="mg/L",
                input_evidence_ref=dependency,
                conversion_id="unit:mg-l->mg-l:v1",
            ),
            source_record_refs=(),
            derivation=EvidenceDerivation(
                algorithm="decimal_unit_conversion",
                algorithm_version=1,
                policy_id="unit:mg-l->mg-l:v1",
                registry_digest="registry",
            ),
            provenance={"operation_id": "conversion"},
            result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        )
        store.seed_fragment(reference, fragment)
        store.seed_scope(reference.item_id)
    with pytest.raises(EvidenceScopeViolationError):
        await EvidenceBundleBuilder(reader=store, writer=store).build(
            evidence_scope,
            fragment_refs=references,
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )


@run_async
async def test_bundle_serialization_is_order_independent_and_write_failure_is_not_committed(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    refs = (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    first = await builder.build(
        evidence_scope,
        fragment_refs=refs,
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    second = await builder.build(
        evidence_scope,
        fragment_refs=tuple(reversed(refs)),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    assert canonical_model_bytes(first.bundle) == canonical_model_bytes(second.bundle)

    values = first.bundle.model_dump(mode="python")
    values["fragments"] = []
    with pytest.raises(ValidationError):
        EvidenceBundle.model_validate(values)

    store.fail_bundle_write = True
    with pytest.raises(RuntimeError, match="bundle write failed"):
        await builder.build_and_write(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )
    assert store.bundles == []
