from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.application.evidence_tools import EvidenceWindow, build_collection_manifest
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    CollectionStatus,
    EvidenceLocation,
    ParameterKey,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    TimeRange,
    WaterObservationRecord,
)


START = datetime(2026, 9, 29, tzinfo=timezone.utc)
FIRST = TimeRange(start=START, end=START + timedelta(hours=6))
SECOND = TimeRange(
    start=START + timedelta(hours=6),
    end=START + timedelta(hours=12),
)
REQUEST_RANGE = TimeRange(start=FIRST.start, end=SECOND.end)
WINDOWS = (
    EvidenceWindow(window_id="window-000", time_range=FIRST),
    EvidenceWindow(window_id="window-001", time_range=SECOND),
)


def record(at: datetime = START + timedelta(hours=1)) -> WaterObservationRecord:
    return WaterObservationRecord(
        provider="usgs",
        channel="continuous",
        source_uri="https://example.test/public-record",
        retrieved_at=START + timedelta(days=1),
        observed_time=at,
        location=EvidenceLocation(
            point=None,
            comid="123",
            station_id="USGS-123",
            distance_to_target_km=0,
        ),
        provider_quality=ProviderQuality(approval="approved"),
        raw_unit="mg/L",
        provenance=Provenance(provider_record_id=f"record-{at.hour}"),
        parameter=ParameterKey(code_system="usgs", code="00300"),
        value="7.5",
        medium="water",
        statistic="instantaneous",
    )


def complete_batch(
    records: tuple[WaterObservationRecord, ...],
) -> ProviderCollectionBatch[WaterObservationRecord]:
    return ProviderCollectionBatch[WaterObservationRecord](
        operation_id="operation-1",
        records=records,
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity="page-1",
                record_count=len(records),
                response_bytes=128,
                page_complete=True,
            ),
        ),
        completed_ranges=(REQUEST_RANGE,),
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=128,
        ),
        completion_proof=CollectionCompletionProof(
            contract="official_next_chain",
            complete=True,
            terminal_identity="page-1",
            digest="complete-digest",
        ),
    )


def manifest_for(batch):
    return build_collection_manifest(
        manifest_id="manifest-1",
        provider="usgs",
        channel="continuous",
        request_range=REQUEST_RANGE,
        requested_series=("series-123",),
        windows=WINDOWS,
        batch=batch,
    )


def test_complete_collection_is_separate_from_observation_coverage() -> None:
    batch = complete_batch((record(),))
    manifest = manifest_for(batch)

    assert manifest.collection_status is CollectionStatus.AVAILABLE
    assert manifest.completion_proof.complete
    assert manifest.counts == CollectionCounts(
        requests=1,
        pages=1,
        records=1,
        response_bytes=128,
    )
    by_window = {item.window_id: item for item in manifest.window_completeness}
    assert by_window["window-000"].status is CollectionStatus.AVAILABLE
    assert by_window["window-000"].complete
    assert by_window["window-001"].status is CollectionStatus.EMPTY
    assert by_window["window-001"].complete


def test_successful_empty_requires_a_completed_official_contract() -> None:
    manifest = manifest_for(complete_batch(()))

    assert manifest.collection_status is CollectionStatus.EMPTY
    assert manifest.completion_proof.complete
    assert all(item.complete for item in manifest.window_completeness)
    assert all(
        item.status is CollectionStatus.EMPTY
        for item in manifest.window_completeness
    )


def test_failed_page_blocks_only_unproven_window_and_retains_parsed_records() -> None:
    parsed = record()
    batch = ProviderCollectionBatch[WaterObservationRecord](
        operation_id="operation-partial",
        records=(parsed,),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity="page-1",
                record_count=1,
                response_bytes=100,
                page_complete=True,
                next_page_identity="page-2",
            ),
            CollectionPageManifest(
                page_identity="page-2",
                record_count=0,
                response_bytes=20,
                page_complete=False,
            ),
        ),
        completed_ranges=(FIRST,),
        counts=CollectionCounts(
            requests=2,
            pages=2,
            records=1,
            response_bytes=120,
        ),
        completion_proof=CollectionCompletionProof(
            contract="official_next_chain",
            complete=False,
            digest="partial-digest",
        ),
        incomplete_reason="provider page unavailable",
    )
    manifest = manifest_for(batch)

    assert manifest.collection_status is CollectionStatus.UNAVAILABLE
    by_window = {item.window_id: item for item in manifest.window_completeness}
    assert by_window["window-000"].complete
    assert by_window["window-000"].status is CollectionStatus.AVAILABLE
    assert not by_window["window-001"].complete
    assert by_window["window-001"].reason == "incomplete_source"
    assert batch.records[0].record_id == parsed.record_id
    assert manifest.counts.requests == 2


def test_batch_rejects_forged_counts_and_complete_incomplete_pages() -> None:
    payload = complete_batch((record(),)).model_dump(mode="python")
    payload["counts"]["records"] = 2
    with pytest.raises(ValidationError, match="record count"):
        ProviderCollectionBatch[WaterObservationRecord].model_validate(payload)

    payload = complete_batch((record(),)).model_dump(mode="python")
    payload["page_or_chunk_manifests"][0]["page_complete"] = False
    with pytest.raises(ValidationError, match="every page"):
        ProviderCollectionBatch[WaterObservationRecord].model_validate(payload)


def test_manifest_rejects_completed_ranges_outside_authorized_request() -> None:
    batch = complete_batch(())
    forged = batch.model_copy(
        update={
            "completed_ranges": (
                TimeRange(
                    start=REQUEST_RANGE.start - timedelta(seconds=1),
                    end=REQUEST_RANGE.end,
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="within the provider request"):
        manifest_for(forged)
