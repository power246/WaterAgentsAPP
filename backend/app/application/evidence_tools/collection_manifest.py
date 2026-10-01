from app.application.evidence_tools.errors import EvidenceCollectionIncompleteError
from app.application.evidence_tools.scope import EvidenceWindow
from app.domain import (
    CollectionManifest,
    CollectionStatus,
    ProviderCollectionBatch,
    SourceRecord,
    TimeRange,
    WindowCompleteness,
)


def build_collection_manifest(
    *,
    manifest_id: str,
    provider: str,
    channel: str,
    request_range: TimeRange,
    requested_series: tuple[str, ...],
    windows: tuple[EvidenceWindow, ...],
    batch: ProviderCollectionBatch[SourceRecord],
    completion_applies_to_all_windows: bool = False,
) -> CollectionManifest:
    if not windows:
        raise ValueError("collection manifest requires affected windows")
    for completed in batch.completed_ranges:
        if completed.start < request_range.start or completed.end > request_range.end:
            raise ValueError("completed ranges must stay within the provider request")

    completeness = tuple(
        _window_completeness(
            window,
            batch,
            completion_applies_to_all_windows=completion_applies_to_all_windows,
        )
        for window in windows
    )
    all_complete = all(item.complete for item in completeness)
    if batch.completion_proof.complete != all_complete:
        if batch.completion_proof.complete:
            raise ValueError("provider completion does not cover every affected window")
        # A failed whole-operation proof cannot silently certify every window.
        completeness = tuple(
            WindowCompleteness(
                window_id=window.window_id,
                complete=False,
                status=CollectionStatus.UNAVAILABLE,
                reason="incomplete_source",
            )
            for window in windows
        )
        all_complete = False

    if not all_complete:
        status = CollectionStatus.UNAVAILABLE
    elif batch.records:
        status = CollectionStatus.AVAILABLE
    else:
        status = CollectionStatus.EMPTY
    return CollectionManifest(
        manifest_id=manifest_id,
        operation_id=batch.operation_id,
        provider=provider,
        channel=channel,
        collection_status=status,
        request_range=request_range,
        requested_series=requested_series,
        page_or_chunk_manifests=batch.page_or_chunk_manifests,
        completed_ranges=batch.completed_ranges,
        affected_window_ids=tuple(window.window_id for window in windows),
        window_completeness=completeness,
        counts=batch.counts,
        completion_proof=batch.completion_proof,
    )


def require_complete_manifest(manifest: CollectionManifest) -> None:
    if not manifest.completion_proof.complete:
        raise EvidenceCollectionIncompleteError()


def _window_completeness(
    window: EvidenceWindow,
    batch: ProviderCollectionBatch[SourceRecord],
    *,
    completion_applies_to_all_windows: bool = False,
) -> WindowCompleteness:
    complete = (
        batch.completion_proof.complete
        if completion_applies_to_all_windows
        else _range_is_covered(window.time_range, batch.completed_ranges)
    )
    if not complete:
        return WindowCompleteness(
            window_id=window.window_id,
            complete=False,
            status=CollectionStatus.UNAVAILABLE,
            reason="incomplete_source",
        )
    has_records = any(
        _record_overlaps(record, window.time_range) for record in batch.records
    )
    return WindowCompleteness(
        window_id=window.window_id,
        complete=True,
        status=(CollectionStatus.AVAILABLE if has_records else CollectionStatus.EMPTY),
    )


def _range_is_covered(
    target: TimeRange,
    completed_ranges: tuple[TimeRange, ...],
) -> bool:
    cursor = target.start
    for completed in completed_ranges:
        if completed.end <= cursor:
            continue
        if completed.start > cursor:
            return False
        cursor = max(cursor, completed.end)
        if cursor >= target.end:
            return True
    return False


def _record_overlaps(record: SourceRecord, window: TimeRange) -> bool:
    if record.observed_time is not None:
        return window.contains(record.observed_time)
    if record.valid_time_range is not None:
        return window.overlaps(record.valid_time_range)
    return False
