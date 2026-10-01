import asyncio
from datetime import timedelta
from functools import wraps

import pytest

from app.application.evidence_tools import (
    EvidenceCollectionInput,
    EvidenceCollectionService,
    EvidenceToolLimits,
)
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    CollectionStatus,
    ContextPayload,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ParameterProvider,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    TimeRange,
    WaterObservationRecord,
    canonical_model_bytes,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.fakes import MemoryEvidenceStore


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
