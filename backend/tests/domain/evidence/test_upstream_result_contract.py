from datetime import datetime, timezone

from app.domain import (
    EvidenceLocation,
    ItemReference,
    ParameterKey,
    Provenance,
    ProviderQuality,
    TimeRange,
    UpstreamResultRecord,
)


def test_upstream_record_keeps_result_and_closure_references_distinct() -> None:
    record = UpstreamResultRecord(
        record_id="upstream:item-result:cell-1",
        provider="analysis_run",
        channel="upstream_result",
        source_uri=None,
        retrieved_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        valid_time_range=TimeRange(
            start=datetime(2026, 10, 1, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, tzinfo=timezone.utc),
        ),
        location=EvidenceLocation(
            point=None,
            comid="100",
            station_id=None,
            distance_to_target_km=None,
        ),
        provider_quality=ProviderQuality(),
        provenance=Provenance(
            operation_id="operation-001",
            provider_record_id="upstream:item-result:cell-1",
        ),
        upstream_session_id="session-100",
        result_ref=ItemReference(item_id="item-result"),
        parameter=ParameterKey(code_system="usgs", code="00010"),
        window_id="window-000",
        result_kind="directional",
        result_source="inferred",
        confidence=0.5,
        evidence_closure_refs=(ItemReference(item_id="item-evidence"),),
    )

    assert record.referenced_item_ids() == ("item-result", "item-evidence")
