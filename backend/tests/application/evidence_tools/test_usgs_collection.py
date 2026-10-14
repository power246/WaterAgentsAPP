import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.application.evidence_tools import EvidenceWindow, build_collection_manifest
from app.domain import (
    CollectionStatus,
    ParameterKey,
    ParameterProvider,
    TimeRange,
    WaterObservationRequest,
)
from app.infrastructure.adapters import UsgsObservationAdapter
from app.infrastructure.http import ProviderHttpClient

FIXTURES = Path(__file__).parents[2] / "infrastructure" / "fixtures"
OGC_BASE = "https://provider.test/ogcapi/v1"
SAMPLES_BASE = "https://provider.test/samples-data"
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
MID = START + timedelta(days=1)
END = START + timedelta(days=2)


def run(coroutine):
    return asyncio.run(coroutine)


def payload(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def request() -> WaterObservationRequest:
    return WaterObservationRequest(
        operation_id="m6-application-collection",
        provider=ParameterProvider.WDFN_TIME_SERIES,
        channel="continuous",
        station_id="USGS-01646500",
        series_refs=("series-continuous-00300",),
        parameter_keys=(ParameterKey(code_system="usgs", code="00300"),),
        request_range=TimeRange(start=START, end=END),
        analysis_timezone="UTC",
        affected_window_ids=("window-1", "window-2"),
    )


WINDOWS = (
    EvidenceWindow(
        window_id="window-1",
        time_range=TimeRange(start=START, end=MID),
        segment_index=0,
    ),
    EvidenceWindow(
        window_id="window-2",
        time_range=TimeRange(start=MID, end=END),
        segment_index=0,
    ),
)


def test_partial_usgs_collection_keeps_records_but_blocks_all_affected_windows() -> None:
    first = payload("usgs_continuous_page_1.json")

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.params.get("cursor"):
            return httpx.Response(200, content=b"{", request=http_request)
        return httpx.Response(200, json=first, request=http_request)

    async def collect():
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=200_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            return await UsgsObservationAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
                clock=lambda: datetime(2024, 1, 4, tzinfo=timezone.utc),
            ).collect(request())

    batch = run(collect())
    manifest = build_collection_manifest(
        manifest_id="manifest-partial-usgs",
        provider="usgs_waterdata",
        channel="continuous",
        request_range=request().request_range,
        requested_series=request().series_refs,
        windows=WINDOWS,
        batch=batch,
    )
    assert len(batch.records) == 1
    assert manifest.collection_status is CollectionStatus.UNAVAILABLE
    assert manifest.completion_proof.complete is False
    assert manifest.completed_ranges == ()
    assert all(not item.complete for item in manifest.window_completeness)
    assert all(
        item.reason == "incomplete_source"
        for item in manifest.window_completeness
    )


def test_complete_empty_usgs_chain_marks_each_covered_window_empty() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": [], "links": []},
            request=http_request,
        )

    async def collect():
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=200_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            return await UsgsObservationAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            ).collect(request())

    batch = run(collect())
    manifest = build_collection_manifest(
        manifest_id="manifest-empty-usgs",
        provider="usgs_waterdata",
        channel="continuous",
        request_range=request().request_range,
        requested_series=request().series_refs,
        windows=WINDOWS,
        batch=batch,
    )
    assert manifest.collection_status is CollectionStatus.EMPTY
    assert manifest.completion_proof.complete is True
    assert all(item.complete for item in manifest.window_completeness)
    assert all(
        item.status is CollectionStatus.EMPTY for item in manifest.window_completeness
    )


def test_m6_adapter_layer_does_not_construct_fragment_bundle_or_agent_output() -> None:
    source = (
        Path(__file__).parents[3]
        / "app"
        / "infrastructure"
        / "adapters"
        / "usgs_observations.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("EvidenceFragment", "EvidenceBundle", "InferenceResult"):
        assert forbidden not in source
