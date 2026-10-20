import asyncio
from dataclasses import replace
from datetime import timedelta
from functools import wraps

import pytest

from app.application.evidence_policy import resolve_applicability
from app.application.evidence_tools import (
    EvidenceCollectionInput,
    EvidenceCollectionService,
    EvidenceToolLimits,
)
from app.application.evidence_tools.collection_service import build_policy_observation
from app.application.evidence_tools.relation_materializer import (
    EvidenceRelationMaterializer,
)
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionFactPayload,
    CollectionPageManifest,
    CollectionStatus,
    ContextPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceApplicability,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    GeoPoint,
    HistoricalInferencePayload,
    ItemReference,
    OutcomeStatus,
    ParameterKey,
    ParameterProvider,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    RelationSupportPayload,
    ResultSource,
    RiskLevel,
    SourceRecordReference,
    TimeRange,
    UpstreamResultCellProjection,
    UpstreamResultPayload,
    UpstreamResultRecord,
    WaterObservationRecord,
    canonical_model_bytes,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import make_context
from tests.application.evidence_tools.fakes import MemoryEvidenceStore
from tests.domain.test_evidence import valid_source_records


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def observation(
    scope,
    index,
    *,
    parameter=None,
    window_index=0,
    approval="approved",
    unit=None,
    method="sensor",
    statistic="instantaneous",
    page="page-1",
):
    parameter = parameter or scope.requested_parameters[0]
    window = scope.time_windows[window_index].time_range
    is_discharge = parameter == scope.supporting_parameters[0]
    record_type = DischargeObservationRecord if is_discharge else WaterObservationRecord
    return record_type(
        provider="usgs_waterdata",
        channel="continuous",
        source_uri="https://example.test/public",
        retrieved_at=scope.query_envelope.end,
        observed_time=window.start + timedelta(minutes=10 + index),
        location=EvidenceLocation(
            point=None,
            comid=None,
            station_id="USGS-123",
            distance_to_target_km=0,
        ),
        provider_quality=ProviderQuality(approval=approval),
        raw_unit=unit or ("ft3/s" if is_discharge else "mg/L"),
        provenance=Provenance(
            operation_id="provider-operation",
            request_identity="request-1",
            page_identity=page,
            provider_series_id=("series-flow" if is_discharge else "series-do"),
            provider_record_id=f"provider-record-{index:03d}",
        ),
        parameter=parameter,
        value=str(7 + index),
        medium="Water",
        fraction=None if is_discharge else "Dissolved",
        method=method,
        statistic=statistic,
    )


def batch(scope, records=(), *, operation_id="provider-1", complete=True, completed=None):
    affected = scope.time_windows[:2]
    if completed is None:
        completed = (
            TimeRange(
                start=affected[0].time_range.start,
                end=affected[-1].time_range.end,
            ),
        ) if complete else (affected[0].time_range,)
    page_id = f"page:{operation_id}"
    return ProviderCollectionBatch(
        operation_id=operation_id,
        records=tuple(records),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity=page_id,
                record_count=len(records),
                response_bytes=max(1, sum(len(canonical_model_bytes(item)) for item in records)),
                page_complete=complete,
            ),
        ),
        completed_ranges=completed,
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=max(1, sum(len(canonical_model_bytes(item)) for item in records)),
        ),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=complete,
            terminal_identity=page_id if complete else None,
            digest=f"digest:{operation_id}",
        ),
        incomplete_reason=None if complete else "incomplete_source",
    )


def collection(scope, provider_batch, *, parameters=None):
    return EvidenceCollectionInput(
        provider=ParameterProvider.WDFN_TIME_SERIES.value,
        channel="continuous",
        request_range=scope.query_envelope,
        requested_series=("series-do", "series-flow"),
        parameter_keys=parameters or (
            scope.requested_parameters[0],
            scope.supporting_parameters[0],
        ),
        affected_window_ids=tuple(item.window_id for item in scope.time_windows[:2]),
        batch=provider_batch,
        station_id="USGS-123",
    )


def fragments(store):
    return tuple(store.fragments.values())


@run_async
async def test_direct_measurement_uses_station_to_query_point_on_saved_reach(
    scope_factory,
) -> None:
    context = make_context()
    target_point = GeoPoint(latitude=39.0, longitude=-77.002)
    context = context.model_copy(update={
        "target": context.target.model_copy(update={"point": target_point})
    })
    scope = scope_factory(context=context)
    station_point = GeoPoint(latitude=39.0, longitude=-77.005)
    original = observation(scope, 1)
    source = original.model_copy(update={
        "location": original.location.model_copy(update={
            "point": station_point,
            "distance_to_target_km": None,
        }),
    })
    store = MemoryEvidenceStore()
    await EvidenceCollectionService(
        registry=load_parameter_capability_registry(), writer=store
    ).process(
        scope,
        operation_id="direct-distance",
        collections=(collection(
            scope, batch(scope, (source,), operation_id="direct-distance")
        ),),
    )
    direct = next(
        fragment for fragment in fragments(store)
        if isinstance(fragment.payload, DirectRecordPayload)
    )
    assert direct.location.point == station_point
    assert direct.location.distance_basis == "mapped_channel_estimate"
    assert direct.location.distance_to_target_km is not None
    assert direct.location.distance_to_target_km > 0
    assert direct.location.distance_to_target_km < 0.3


@run_async
async def test_direct_measurement_with_unmapped_point_keeps_distance_unknown(
    scope_factory,
) -> None:
    scope = scope_factory(with_geometry=False)
    original = observation(scope, 1)
    source = original.model_copy(update={
        "location": original.location.model_copy(update={
            "point": GeoPoint(latitude=39.0, longitude=-77.005),
        }),
    })
    store = MemoryEvidenceStore()
    await EvidenceCollectionService(
        registry=load_parameter_capability_registry(), writer=store
    ).process(
        scope,
        operation_id="unmapped-distance",
        collections=(collection(
            scope, batch(scope, (source,), operation_id="unmapped-distance")
        ),),
    )
    direct = next(
        fragment for fragment in fragments(store)
        if isinstance(fragment.payload, DirectRecordPayload)
    )
    assert direct.location.distance_to_target_km is None
    assert direct.location.distance_basis is None


@pytest.mark.parametrize(
    ("code", "raw_method", "expected_fraction", "unit"),
    (
        ("00400", "STNRD", "Total", "std units"),
        ("00060", None, None, "ft3/s"),
    ),
)
def test_verified_usgs_continuous_codes_map_only_in_policy_view(
    evidence_scope, code, raw_method, expected_fraction, unit
):
    registry = load_parameter_capability_registry()
    key = ParameterKey(code_system="usgs", code=code)
    source = observation(
        evidence_scope,
        1,
        parameter=key,
        approval="provisional",
        unit=unit,
    ).model_copy(
        update={
            "medium": "surface water",
            "fraction": None,
            "method": raw_method,
            "statistic": "00011",
        }
    )
    reference = SourceRecordReference(item_id="item-source", record_id=source.record_id)
    policy_view = build_policy_observation(
        registry,
        source,
        reference,
        provider_name=ParameterProvider.WDFN_TIME_SERIES.value,
    )
    assert policy_view is not None
    assert (policy_view.medium, policy_view.fraction) == ("Water", expected_fraction)
    assert (policy_view.method, policy_view.statistic) == ("sensor", "instantaneous")
    assert policy_view.quality.quality_class is EvidenceQualityClass.SUSPICIOUS
    assert (source.medium, source.fraction, source.method, source.statistic) == (
        "surface water", None, raw_method, "00011"
    )

    capability = registry.get(key)
    assert capability is not None
    window = evidence_scope.time_windows[0].time_range
    assert resolve_applicability(
        registry,
        capability,
        evidence_scope.agent_comid,
        window,
        observation=policy_view,
        station_segment_confirmed=True,
        collection_complete=True,
        reference_complete=True,
    ) is EvidenceApplicability.DIRECT_MEASUREMENT
    assert resolve_applicability(
        registry,
        capability,
        evidence_scope.agent_comid,
        window,
        observation=policy_view,
        station_segment_confirmed=False,
        collection_complete=True,
        reference_complete=True,
    ) is EvidenceApplicability.CONTEXTUAL_ONLY

    unknown_statistic = source.model_copy(update={"statistic": "00003"})
    unknown_view = build_policy_observation(
        registry,
        unknown_statistic,
        reference,
        provider_name=ParameterProvider.WDFN_TIME_SERIES.value,
    )
    assert unknown_view is not None
    assert resolve_applicability(
        registry,
        capability,
        evidence_scope.agent_comid,
        window,
        observation=unknown_view,
        station_segment_confirmed=True,
        collection_complete=True,
        reference_complete=True,
    ) is EvidenceApplicability.CONTEXTUAL_ONLY


@run_async
async def test_historical_records_materialize_one_typed_non_recursive_fragment_per_cell(
    evidence_scope,
):
    historical = valid_source_records()[-1]
    provider_batch = batch(
        evidence_scope,
        (historical,),
        operation_id="historical-operation",
    )
    historical_collection = EvidenceCollectionInput(
        provider="transcript",
        channel="historical",
        request_range=evidence_scope.query_envelope,
        requested_series=("strategy:semantic_case_query",),
        parameter_keys=(evidence_scope.requested_parameters[0],),
        affected_window_ids=(evidence_scope.time_windows[0].window_id,),
        batch=provider_batch,
        completion_applies_to_all_windows=True,
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )

    await service.process(
        evidence_scope,
        operation_id="historical-operation",
        collections=(historical_collection,),
    )

    historical_fragments = tuple(
        item
        for item in fragments(store)
        if isinstance(item.payload, HistoricalInferencePayload)
    )
    assert len(historical_fragments) == len(
        historical.result_projection.cells
    )
    fragment = historical_fragments[0]
    assert fragment.applicability.value == "contextual_only"
    assert fragment.result_eligibility is EvidenceResultEligibility.INFERRED_ONLY
    assert not fragment.rag_eligible
    assert fragment.payload.case_ref == historical.case_ref
    assert fragment.payload.cell_projection == historical.result_projection.cells[0]
    assert fragment.referenced_item_ids() == ()


@run_async
async def test_collection_service_classifies_quality_units_and_window_facts(
    evidence_scope,
):
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )
    records = (
        observation(evidence_scope, 1, approval="approved"),
        observation(evidence_scope, 2, approval="provisional"),
        observation(evidence_scope, 3, approval="invalid", window_index=1),
        observation(evidence_scope, 4, unit="mystery-unit", window_index=1),
    )

    artifacts = await service.process(
        evidence_scope,
        operation_id="collection-1",
        collections=(collection(evidence_scope, batch(evidence_scope, records)),),
    )

    available = [
        item for item in fragments(store) if isinstance(item.payload, DirectRecordPayload)
    ]
    contextual = [
        item for item in fragments(store) if isinstance(item.payload, ContextPayload)
    ]
    facts = [
        item for item in fragments(store) if item.data_type == "collection_fact"
    ]
    assert {item.quality_class for item in available} == {
        EvidenceQualityClass.VALID,
        EvidenceQualityClass.SUSPICIOUS,
    }
    assert all(
        item.result_eligibility is EvidenceResultEligibility.OBSERVED_OR_INFERRED
        for item in available
    )
    assert {item.quality_class for item in contextual} == {
        EvidenceQualityClass.INVALID
    }
    assert any(DataQualityFlag.UNIT_UNRESOLVED in item.quality_flags for item in contextual)
    assert any(item.payload.reason == "unit_unresolved" for item in facts)
    assert len(artifacts.manifest_refs) == 1
    assert artifacts.record_preview is not None
    assert artifacts.record_preview.total_records == 4
    assert artifacts.record_preview.serialized_bytes <= 16_384


@run_async
async def test_successful_empty_and_technical_incomplete_remain_distinct_per_window(
    evidence_scope,
):
    registry = load_parameter_capability_registry()
    empty_store = MemoryEvidenceStore()
    empty = EvidenceCollectionService(registry=registry, writer=empty_store)
    await empty.process(
        evidence_scope,
        operation_id="empty",
        collections=(collection(evidence_scope, batch(evidence_scope)),),
    )
    empty_facts = [item for item in fragments(empty_store) if item.data_type == "collection_fact"]
    assert empty_facts
    assert {item.collection_status for item in empty_facts} == {CollectionStatus.EMPTY}
    assert {item.payload.reason for item in empty_facts} == {
        "no_records_in_completed_query"
    }

    partial_store = MemoryEvidenceStore()
    partial = EvidenceCollectionService(registry=registry, writer=partial_store)
    record = observation(evidence_scope, 10)
    await partial.process(
        evidence_scope,
        operation_id="partial",
        collections=(
            collection(
                evidence_scope,
                batch(
                    evidence_scope,
                    (record,),
                    operation_id="partial-provider",
                    complete=False,
                ),
            ),
        ),
    )
    by_window = {}
    for item in fragments(partial_store):
        if item.data_type == "collection_fact":
            by_window.setdefault(item.payload.affected_window_ids[0], set()).add(
                (item.collection_status, item.payload.reason)
            )
    assert (CollectionStatus.AVAILABLE, "records_collected") in by_window[
        evidence_scope.time_windows[0].window_id
    ]
    assert (CollectionStatus.UNAVAILABLE, "incomplete_source") in by_window[
        evidence_scope.time_windows[1].window_id
    ]


@pytest.mark.parametrize("include_support_record", (False, True))
@run_async
async def test_upstream_collection_facts_match_each_records_parameter(
    evidence_scope, include_support_record
):
    target, support = (
        evidence_scope.requested_parameters[0],
        evidence_scope.supporting_parameters[0],
    )
    window = evidence_scope.time_windows[0]

    def upstream_record(parameter):
        return UpstreamResultRecord(
            provider="analysis_run",
            channel="upstream_result",
            source_uri=None,
            retrieved_at=evidence_scope.query_envelope.end,
            valid_time_range=window.time_range,
            location=EvidenceLocation(
                point=None,
                comid="upstream-123",
                station_id=None,
                distance_to_target_km=1,
            ),
            provider_quality=ProviderQuality(),
            provenance=Provenance(
                operation_id="upstream-collection",
                provider_record_id=f"upstream:{parameter.code}",
            ),
            upstream_session_id="upstream-session",
            result_ref=ItemReference(item_id=f"result-{parameter.code}"),
            parameter=parameter,
            window_id=window.window_id,
            result_kind="quantified",
            result_source=ResultSource.INFERRED,
            confidence=0.7,
            evidence_closure_refs=(
                ItemReference(item_id=f"bundle-{parameter.code}"),
            ),
        )

    target_record = upstream_record(target)
    records = (
        (target_record, upstream_record(support))
        if include_support_record
        else (target_record,)
    )
    upstream_collection = replace(
        collection(
            evidence_scope,
            batch(evidence_scope, records, operation_id="upstream-collection"),
        ),
        provider="analysis_run",
        channel="upstream_result",
        requested_series=("upstream-123",),
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(), writer=store
    )
    await service.process(
        evidence_scope,
        operation_id="upstream-collection",
        collections=(upstream_collection,),
    )

    facts = {
        (item.parameter_refs[0], item.payload.affected_window_ids[0]): item
        for item in fragments(store)
        if isinstance(item.payload, CollectionFactPayload)
    }
    target_fact = facts[target, window.window_id]
    support_fact = facts[support, window.window_id]
    assert target_fact.collection_status is CollectionStatus.AVAILABLE
    assert target_fact.payload.reason == "records_collected"
    assert {ref.record_id for ref in target_fact.payload.record_refs} == {
        target_record.record_id
    }
    if include_support_record:
        assert support_fact.collection_status is CollectionStatus.AVAILABLE
        assert support_fact.payload.reason == "records_collected"
        assert {ref.record_id for ref in support_fact.payload.record_refs} == {
            records[1].record_id
        }
    else:
        assert support_fact.collection_status is CollectionStatus.EMPTY
        assert support_fact.payload.reason == "no_records_in_completed_query"
        assert support_fact.payload.record_refs == ()


@pytest.mark.parametrize("complete", (True, False))
@pytest.mark.parametrize("source_flagged", (True, False))
@run_async
async def test_upstream_range_keeps_bounds_for_downstream_estimation(
    evidence_scope, complete, source_flagged, scope_factory, context_factory,
):
    parameter = evidence_scope.requested_parameters[0]
    window = evidence_scope.time_windows[0]
    projection = UpstreamResultCellProjection(
        parameter=parameter,
        window_id=window.window_id,
        time_range=window.time_range,
        outcome_status=OutcomeStatus.RANGE_ESTIMATE,
        representative_value=None,
        value_lower="6.4",
        value_upper="8.1",
        unit_id="standard_units",
        trend=None,
        risk_level=RiskLevel.INDETERMINATE,
        source=ResultSource.INFERRED,
        confidence=0.3,
        quality_flags=(
            DataQualityFlag.ESTIMATED,
            DataQualityFlag.ANOMALOUS,
            *((DataQualityFlag.PROVIDER_FLAGGED,) if source_flagged else ()),
        ),
        inference_method="model_estimate",
        inference_basis="related_evidence",
    )
    record = UpstreamResultRecord(
        provider="analysis_run",
        channel="upstream_result",
        source_uri=None,
        retrieved_at=evidence_scope.query_envelope.end,
        valid_time_range=window.time_range,
        location=EvidenceLocation(
            point=None, comid="200", station_id=None,
            distance_to_target_km=1,
        ),
        provider_quality=ProviderQuality(),
        provenance=Provenance(
            operation_id="upstream-range",
            provider_record_id="upstream-range:ph",
        ),
        upstream_session_id="upstream-session",
        result_ref=ItemReference(item_id="upstream-result"),
        parameter=parameter,
        window_id=window.window_id,
        result_kind="range_estimate",
        result_source=ResultSource.INFERRED,
        confidence=0.3,
        evidence_closure_refs=(ItemReference(item_id="upstream-bundle"),),
        cell_projection=projection,
    )
    upstream_collection = replace(
        collection(
            evidence_scope,
            batch(
                evidence_scope, (record,), operation_id="upstream-range",
                complete=complete, completed=None if complete else (),
            ),
        ),
        provider="analysis_run",
        channel="upstream_result",
        requested_series=("200",),
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(), writer=store
    )
    artifacts = await service.process(
        evidence_scope,
        operation_id="upstream-range",
        collections=(upstream_collection,),
    )
    carried = [
        item
        for item in fragments(store)
        if isinstance(item.payload, UpstreamResultPayload)
    ]
    assert len(carried) == 1
    assert carried[0].coverage.status is (
        CoverageStatus.COMPLETE if complete else CoverageStatus.UNKNOWN
    )
    assert DataQualityFlag.PARTIAL_COVERAGE not in carried[0].quality_flags
    assert set(carried[0].quality_flags) == set(projection.quality_flags)
    assert carried[0].payload.cell_projection == projection
    assert carried[0].payload.cell_projection.representative_value is None
    if complete:
        rule = create_evidence_rule(
            output_parameter=parameter,
            input_parameters=(parameter,),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.UPSTREAM_TO_DOWNSTREAM,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=True,
            rationale="Transfer an upstream range to the downstream reach.",
            origin=EvidenceRuleOrigin.QUERY_PARSER,
        )
        relation_scope = scope_factory(
            context=context_factory(
                evidence_rule_plan=create_evidence_rule_plan((rule,)),
            ),
            with_upstream=True,
        )
        await EvidenceRelationMaterializer(
            registry=load_parameter_capability_registry(),
            reader=store,
            writer=store,
        ).materialize(
            relation_scope, artifacts.fragment_refs, operation_id="upstream-relation",
        )
        relations = tuple(
            item for item in fragments(store)
            if isinstance(item.payload, RelationSupportPayload)
        )
        assert len(relations) == 1
        assert set(relations[0].quality_flags) == set(carried[0].quality_flags)


@run_async
async def test_stable_identity_deduplicates_only_same_record_and_merges_discovery(
    evidence_scope,
):
    original = observation(evidence_scope, 20, page="page-a")
    duplicate = original.model_copy(
        update={
            "retrieved_at": original.retrieved_at + timedelta(seconds=1),
            "provenance": original.provenance.model_copy(update={"page_identity": "page-b"}),
        }
    )
    different_series = observation(evidence_scope, 21, method="laboratory")
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )
    artifacts = await service.process(
        evidence_scope,
        operation_id="dedup",
        collections=(
            collection(evidence_scope, batch(evidence_scope, (original, different_series), operation_id="a")),
            collection(evidence_scope, batch(evidence_scope, (duplicate,), operation_id="b")),
        ),
    )

    assert len(artifacts.source_record_refs) == 2
    record_fragments = [item for item in fragments(store) if item.data_type == "water_observation"]
    duplicate_fragment = next(
        item
        for item in record_fragments
        if item.source_record_refs[0].record_id == original.record_id
    )
    assert {"page:page-a", "page:page-b"} <= set(
        duplicate_fragment.provenance.discovery_identities
    )
    assert len({item.series_identity for item in record_fragments}) == 2


@run_async
async def test_preview_and_serialization_are_bounded_and_order_independent(evidence_scope):
    records = tuple(observation(evidence_scope, index) for index in range(40))
    limits = EvidenceToolLimits(preview_max_records=32, preview_max_bytes=4_096)
    outputs = []
    for ordered in (records, tuple(reversed(records))):
        store = MemoryEvidenceStore()
        service = EvidenceCollectionService(
            registry=load_parameter_capability_registry(),
            writer=store,
            limits=limits,
        )
        artifacts = await service.process(
            evidence_scope,
            operation_id="deterministic",
            collections=(collection(evidence_scope, batch(evidence_scope, ordered)),),
        )
        preview = artifacts.record_preview
        assert preview is not None
        assert len(preview.records) <= 32
        assert preview.serialized_bytes <= 4_096
        outputs.append(
            (
                tuple(canonical_model_bytes(item) for item in store.manifests.values()),
                tuple(
                    canonical_model_bytes(item)
                    for item in sorted(fragments(store), key=lambda value: value.evidence_id)
                ),
            )
        )
    assert outputs[0] == outputs[1]
