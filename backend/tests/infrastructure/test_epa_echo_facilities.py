import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.domain import FacilityEvidenceRequest, GeoPoint, WaterbodyGeometry
from app.infrastructure.adapters import EpaEchoFacilityAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortConfigurationError, PortResponseLimitError

FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run(coroutine):
    return asyncio.run(coroutine)


def provider_client(handler) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=200_000,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def geometry() -> WaterbodyGeometry:
    return WaterbodyGeometry.from_coordinates(
        source_comid="123",
        coordinates=((0.0, 0.0), (0.01, 0.0)),
    )


def facility_request() -> FacilityEvidenceRequest:
    return FacilityEvidenceRequest(
        operation_id="facility-collect",
        geometry_ref="geometry:123",
        exact_radius_miles=1,
        provider_radius_miles=2,
        sample_centers=(
            GeoPoint(latitude=0.0, longitude=0.0),
            GeoPoint(latitude=0.0, longitude=0.01),
        ),
        affected_window_ids=("w000",),
    )


def test_multicenter_pagination_dedup_exact_filter_and_missing_coordinates() -> None:
    requests: list[httpx.Request] = []
    initial_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initial_calls
        requests.append(request)
        if request.url.path.endswith("get_qid"):
            payload = fixture("epa_facilities_page_2.json")
        else:
            initial_calls += 1
            payload = fixture(
                "epa_facilities_page_1.json"
                if initial_calls == 1
                else "epa_facilities_duplicate.json"
            )
        return httpx.Response(200, json=payload, request=request)

    current_geometry = geometry()

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: (
                    current_geometry if value == current_geometry.geometry_ref else None
                ),
                response_set=2,
                clock=lambda: NOW,
            ).collect(facility_request())
        assert batch.completion_proof.complete is True
        assert len(batch.records) == 2
        inside, missing = batch.records
        assert inside.facility_id == "MD0000001"
        assert inside.applicability == "contextual_only"
        assert inside.spatially_in_scope is True
        assert inside.location.distance_to_target_km is not None
        assert inside.location.distance_to_target_km < 1.609344
        assert inside.provenance.facility_agent_radius_miles == "1"
        assert inside.provenance.facility_provider_radius_miles == "2"
        assert (
            inside.provenance.facility_sample_centers
            == facility_request().sample_centers
        )
        assert len(inside.provenance.facility_request_identities) == 2
        assert inside.provenance.facility_deduplicated_count == 1
        assert (
            inside.provenance.facility_final_distance_km
            == inside.location.distance_to_target_km
        )
        assert missing.facility_id == "MD0000002"
        assert missing.location.point is None
        assert missing.spatially_in_scope is False
        assert missing.location.distance_to_target_km is None
        assert missing.provider_quality.qualifiers == ("missing_coordinates",)
        assert "MD0000003" not in {item.facility_id for item in batch.records}

    run(exercise())
    assert len(requests) == 3
    first = requests[0]
    assert first.method == "GET"
    assert first.url.path == "/echo/cwa_rest_services.get_facilities"
    assert first.url.params["output"] == "JSON"
    assert first.url.params["p_radius"] == "2.0"
    assert first.url.params["responseset"] == "2"
    assert requests[1].url.path == "/echo/cwa_rest_services.get_qid"
    assert requests[1].url.params["qid"] == "qid-101"
    assert requests[1].url.params["pageno"] == "2"
    assert all(request.url.host == "echodata.epa.gov" for request in requests)


def test_provider_failure_after_confirmed_pages_returns_typed_partial_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = fixture("epa_facilities_duplicate.json")
            return httpx.Response(200, json=payload, request=request)
        return httpx.Response(503, json={"error": "unavailable"}, request=request)

    current_geometry = geometry()

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: current_geometry,
                response_set=2,
                clock=lambda: NOW,
            ).collect(facility_request())
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_unavailable"
        assert [item.facility_id for item in batch.records] == ["MD0000001"]
        assert batch.page_or_chunk_manifests[-1].page_complete is False

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert calls == 3
    assert "unavailable" not in caplog.text
    assert "secret" not in caplog.text


def test_page_and_record_limits_fail_before_silent_truncation() -> None:
    payload = fixture("epa_facilities_page_1.json")
    payload["Results"]["QueryRows"] = "2001"
    current_geometry = geometry()

    async def exercise() -> None:
        async with provider_client(
            lambda request: httpx.Response(200, json=payload, request=request)
        ) as http:
            adapter = EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: current_geometry,
                response_set=1000,
                max_pages=2,
                max_records=10_000,
                clock=lambda: NOW,
            )
            with pytest.raises(PortResponseLimitError):
                await adapter.collect(facility_request())

    run(exercise())


def test_page_limit_is_global_across_centers_and_blocks_extra_io() -> None:
    current_geometry = geometry()
    provider_requests: list[httpx.Request] = []
    request = facility_request().model_copy(
        update={
            "sample_centers": (
                GeoPoint(latitude=0.0, longitude=0.0),
                GeoPoint(latitude=0.0, longitude=0.005),
                GeoPoint(latitude=0.0, longitude=0.01),
            )
        }
    )

    def handler(provider_request: httpx.Request) -> httpx.Response:
        provider_requests.append(provider_request)
        return httpx.Response(
            200,
            json={
                "Results": {
                    "Message": "Success",
                    "Version": "v2026",
                    "QueryRows": "0",
                    "QueryID": f"qid-{len(provider_requests)}",
                    "PageNo": "1",
                    "Facilities": [],
                }
            },
            request=provider_request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: current_geometry,
                max_pages=2,
                clock=lambda: NOW,
            ).collect(request)
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_response_too_large"
        assert batch.counts.pages == 2

    run(exercise())
    assert len(provider_requests) == 2


def test_missing_trusted_geometry_fails_before_provider_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={}, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: None,
                clock=lambda: NOW,
            )
            with pytest.raises(PortConfigurationError):
                await adapter.collect(facility_request())

    run(exercise())
    assert requests == []


def test_epa_configuration_rejects_nonofficial_origin() -> None:
    client = provider_client(
        lambda request: httpx.Response(200, json={}, request=request)
    )
    try:
        EpaEchoFacilityAdapter(
            client,
            geometry_resolver=lambda value: None,
            base_url="https://evil.example/echo",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe EPA ECHO origin was accepted")
