import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.domain import GeoPoint
from app.infrastructure.adapters import NldiAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import InvalidPortResponseError, PortHttpError

FIXTURES = Path(__file__).with_name("fixtures")
BASE = "https://provider.test/nldi/linked-data"


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


def test_position_uses_point_in_polygon_with_lon_lat_order() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=fixture("nldi_position.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            result = await NldiAdapter(http, base_url=BASE).lookup_position(
                GeoPoint(latitude=38.75, longitude=-77.25)
            )
            assert result == "12345678"

    run(exercise())
    assert requests[0].url.path.endswith("/comid/position")
    assert requests[0].url.params["coords"] == "POINT(-77.25 38.75)"
    assert "hydrolocation" not in str(requests[0].url)


def test_segment_and_navigation_are_normalized_to_direct_edges() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/navigation/UT/flowlines"):
            return httpx.Response(
                200,
                json=fixture("nldi_upstreams.json"),
                request=request,
            )
        return httpx.Response(
            200,
            json=fixture("nldi_position.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE)
            segment = await adapter.get_segment("12345678")
            upstreams = await adapter.get_direct_upstreams("12345678", 12.5)
            assert segment is not None
            assert segment.comid == "12345678"
            assert segment.geometry is not None
            assert segment.geometry_ref == "geometry:12345678"
            assert segment.geometry_summary == segment.geometry.summary()
            assert segment.location == segment.geometry.representative_point
            assert [item.comid for item in upstreams] == ["11111111", "22222222"]
            assert all(item.downstream_comids == ("12345678",) for item in upstreams)
            assert all(
                item.distance_to_downstream_km is not None
                and item.distance_to_downstream_km > 0
                for item in upstreams
            )
            assert "33333333" not in {item.comid for item in upstreams}

    run(exercise())
    navigation = requests[-1]
    assert navigation.url.params["distance"] == "12.5"
    assert navigation.url.params["excludeGeometry"] == "false"


def test_monitoring_sites_use_provider_comid_association_and_deduplicate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=fixture("nldi_sites.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            stations = await NldiAdapter(
                http,
                base_url=BASE,
            ).get_monitoring_sites("12345678")
            assert len(stations) == 1
            assert stations[0].station_id == "USGS-01646500"
            assert stations[0].associated_comid == "12345678"
            assert stations[0].location == GeoPoint(
                latitude=38.93,
                longitude=-77.03,
            )

    run(exercise())


def test_successful_empty_results_remain_empty() -> None:
    empty = {"type": "FeatureCollection", "features": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=empty, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NldiAdapter(http, base_url=BASE)
            assert await adapter.lookup_position(
                GeoPoint(latitude=38.75, longitude=-77.25)
            ) is None
            assert await adapter.get_segment("12345678") is None
            assert await adapter.get_direct_upstreams("12345678", 1) == ()
            assert await adapter.get_monitoring_sites("12345678") == ()

    run(exercise())


def test_missing_geometry_and_field_drift_are_invalid_not_empty() -> None:
    origin = fixture("nldi_position.json")
    malformed_navigation = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"nhdplus_comid": 11111111},
                "geometry": None,
            }
        ],
    }
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=origin if calls == 1 else malformed_navigation,
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(
                    http,
                    base_url=BASE,
                ).get_direct_upstreams("12345678", 1)

    run(exercise())


def test_segment_with_mismatched_provider_comid_is_invalid() -> None:
    drifted = fixture("nldi_position.json")
    drifted["features"][0]["properties"]["nhdplus_comid"] = 99999999

    async def exercise() -> None:
        async with provider_client(
            lambda request: httpx.Response(200, json=drifted, request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")

    run(exercise())


@pytest.mark.parametrize(
    "geometry",
    (
        {"type": "LineString", "coordinates": []},
        {
            "type": "LineString",
            "coordinates": [[-77.0, 39.0], [-77.0, 39.0]],
        },
        {
            "type": "LineString",
            "coordinates": [[-181.0, 39.0], [-77.0, 39.0]],
        },
        {
            "type": "MultiLineString",
            "coordinates": [[[-77.0, 39.0], [-76.0, 39.0]]],
        },
        {"type": "Point", "coordinates": [-77.0, 39.0]},
        None,
    ),
)
def test_segment_rejects_empty_repeated_invalid_and_unsupported_geometry(
    geometry,
) -> None:
    payload = fixture("nldi_position.json")
    payload["features"][0]["geometry"] = geometry

    async def exercise() -> None:
        async with provider_client(
            lambda request: httpx.Response(200, json=payload, request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")

    run(exercise())


def test_non_success_http_is_not_rewritten_as_no_match() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            with pytest.raises(PortHttpError) as captured:
                await NldiAdapter(http, base_url=BASE).get_segment("12345678")
            assert captured.value.status_code == 404

    run(exercise())
    assert calls == 1
