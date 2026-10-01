import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from app.domain import FacilityEvidenceRequest, GeoPoint, WaterbodyGeometry
from app.infrastructure.adapters import EpaEchoFacilityAdapter
from app.infrastructure.http import ProviderHttpClient

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def empty_result(query_id: str) -> dict[str, object]:
    return {
        "Results": {
            "Message": "Success",
            "Version": "v2026",
            "QueryRows": "0",
            "QueryID": query_id,
            "PageNo": "1",
            "Facilities": [],
        }
    }


def test_all_server_planned_centers_are_queried_without_fallback() -> None:
    geometry = WaterbodyGeometry.from_coordinates(
        source_comid="123",
        coordinates=((0.0, 0.0), (0.01, 0.005), (0.02, 0.0)),
    )
    centers = tuple(
        GeoPoint(latitude=0.001 * index, longitude=0.004 * index)
        for index in range(6)
    )
    request = FacilityEvidenceRequest(
        operation_id="facility-all-centers",
        geometry_ref=geometry.geometry_ref,
        exact_radius_miles=1,
        provider_radius_miles=2,
        sample_centers=centers,
        affected_window_ids=("w000",),
    )
    provider_requests: list[httpx.Request] = []

    def handler(provider_request: httpx.Request) -> httpx.Response:
        provider_requests.append(provider_request)
        return httpx.Response(
            200,
            json=empty_result(f"qid-{len(provider_requests)}"),
            request=provider_request,
        )

    async def exercise() -> None:
        http = ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=200_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        async with http:
            batch = await EpaEchoFacilityAdapter(
                http,
                geometry_resolver=lambda value: geometry,
                clock=lambda: NOW,
            ).collect(request)
        assert batch.completion_proof.complete is True
        assert batch.records == ()

    asyncio.run(exercise())
    assert len(provider_requests) == len(centers)
    queried = tuple(
        GeoPoint(
            latitude=float(item.url.params["p_lat"]),
            longitude=float(item.url.params["p_long"]),
        )
        for item in provider_requests
    )
    assert queried == centers


def test_center_budget_is_rejected_before_any_adapter_can_issue_io() -> None:
    centers = tuple(
        GeoPoint(latitude=0.0, longitude=index / 1000)
        for index in range(33)
    )
    with pytest.raises(ValidationError, match="sample_centers"):
        FacilityEvidenceRequest(
            operation_id="facility-over-budget",
            geometry_ref="geometry:123",
            exact_radius_miles=1,
            provider_radius_miles=1,
            sample_centers=centers,
            affected_window_ids=("w000",),
        )


def test_agent_facing_request_has_no_url_or_center_fields() -> None:
    from app.domain import EvidenceToolRequest

    assert "url" not in EvidenceToolRequest.model_fields
    assert "sample_centers" not in EvidenceToolRequest.model_fields
    assert "geometry_ref" not in EvidenceToolRequest.model_fields
