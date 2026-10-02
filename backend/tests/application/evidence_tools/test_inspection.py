import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.application.evidence_tools import (
    EvidenceInspectionService,
    EvidenceScopeViolationError,
)
from app.domain import (
    EvidenceLocation,
    ParameterKey,
    Provenance,
    ProviderQuality,
    WaterObservationRecord,
)
from tests.application.evidence_tools.fakes import MemoryEvidenceStore


START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def record(index: int, hour: int) -> WaterObservationRecord:
    return WaterObservationRecord(
        provider="usgs",
        channel="continuous",
        source_uri="https://example.test/public",
        retrieved_at=START + timedelta(days=1),
        observed_time=START + timedelta(hours=hour),
        location=EvidenceLocation(
            point=None,
            comid="123",
            station_id="USGS-123",
            distance_to_target_km=0,
        ),
        provider_quality=ProviderQuality(approval="approved"),
        raw_unit="mg/L",
        provenance=Provenance(provider_record_id=f"record-{index:03d}"),
        parameter=ParameterKey(code_system="usgs", code="00300"),
        value=str(index),
        medium="water",
        statistic="instantaneous",
    )


def test_inspection_is_owned_stably_sorted_bounded_and_full_content(
    evidence_scope,
) -> None:
    async def exercise() -> None:
        store = MemoryEvidenceStore()
        source_records = (record(3, 2), record(2, 1), record(1, 1), record(4, 3))
        store.seed_records(evidence_scope, "source-item", source_records)
        service = EvidenceInspectionService(store, default_page_size=2, max_page_size=3)

        first = await service.inspect(evidence_scope, source_item_id="source-item")
        second = await service.inspect(
            evidence_scope,
            source_item_id="source-item",
            offset=first.next_offset,
            limit=3,
        )

        assert [item.record_id for item in first.records] == [
            "record-001",
            "record-002",
        ]
        assert first.records[0].value == "1"
        assert [item.record_id for item in second.records] == [
            "record-003",
            "record-004",
        ]
        assert second.next_offset is None
        assert tuple(ref.record_id for ref in first.record_refs) == (
            "record-001",
            "record-002",
        )
        assert tuple(source_records) == (
            record(3, 2),
            record(2, 1),
            record(1, 1),
            record(4, 3),
        )

    asyncio.run(exercise())


def test_inspection_rejects_cross_scope_items_and_oversized_pages(
    scope_factory,
) -> None:
    async def exercise() -> None:
        owner = scope_factory(run_id="run-owner", session_id="session-owner")
        attacker = scope_factory(run_id="run-other", session_id="session-other")
        store = MemoryEvidenceStore()
        store.seed_records(owner, "source-item", (record(1, 1),))
        service = EvidenceInspectionService(store, default_page_size=1, max_page_size=2)

        with pytest.raises(EvidenceScopeViolationError):
            await service.inspect(attacker, source_item_id="source-item")
        with pytest.raises(EvidenceScopeViolationError):
            await service.inspect(owner, source_item_id="source-item", limit=3)

    asyncio.run(exercise())
