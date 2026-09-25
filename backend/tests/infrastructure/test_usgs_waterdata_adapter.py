import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.domain import ParameterKey, TimeSeriesKind
from app.infrastructure.adapters import UsgsWaterDataAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortResponseLimitError,
    UnsupportedPortVersionError,
)

FIXTURES = Path(__file__).with_name("fixtures")
OGC_BASE = "https://provider.test/ogcapi/v1"
SAMPLES_BASE = "https://provider.test/samples-data"
DISCHARGE = ParameterKey(code_system="usgs", code="00060")


def json_fixture(name: str) -> object:
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


def test_wdfn_parameter_location_and_paginated_series_metadata() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if "parameter-codes" in path:
            payload = json_fixture("wdfn_parameter_codes.json")
        elif "monitoring-locations" in path:
            payload = json_fixture("wdfn_monitoring_locations.json")
        elif request.url.params.get("offset") == "1":
            payload = json_fixture("wdfn_time_series_page_2.json")
        else:
            payload = json_fixture("wdfn_time_series_page_1.json")
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http,
                api_key="secret",
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
                page_size=1,
            )
            parameters = await adapter.get_parameter_codes((DISCHARGE,))
            locations = await adapter.get_monitoring_locations(
                ("USGS-01646500",)
            )
            series = await adapter.get_time_series_metadata(
                ("USGS-01646500",),
                (DISCHARGE,),
            )
            assert parameters[0].key == DISCHARGE
            assert parameters[0].unit == "ft3/s"
            assert locations[0].station_id == "USGS-01646500"
            assert locations[0].location.longitude == -77.03
            assert [item.kind for item in series] == [
                TimeSeriesKind.CONTINUOUS,
                TimeSeriesKind.DAILY,
            ]
            assert all(item.parameter == DISCHARGE for item in series)
            assert all(item.begin <= item.end for item in series)

    run(exercise())
    assert all(request.url.params["api_key"] == "secret" for request in requests)
    first_series = next(
        request for request in requests
        if "time-series-metadata" in request.url.path
        and "offset" not in request.url.params
    )
    second_series = next(
        request for request in requests
        if "time-series-metadata" in request.url.path
        and request.url.params.get("offset") == "1"
    )
    assert first_series.url.params["monitoring_location_id"] == "USGS-01646500"
    assert first_series.url.params["parameter_code"] == "00060"
    assert second_series.url.params["limit"] == "1"


def test_samples_csv_summary_parses_discrete_periods() -> None:
    content = (FIXTURES / "samples_summary.csv").read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/csv"},
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            periods = await UsgsWaterDataAdapter(
                http,
                api_key="secret",
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            ).get_sample_periods("USGS-01646500")
            assert [item.characteristic for item in periods] == [
                "Nitrogen",
                "Temperature",
            ]
            assert periods[0].result_count == 10
            assert periods[1].first_activity.isoformat() == "2024-01-01"

    run(exercise())
    assert requests[0].url.path.endswith(
        "/samples-data/summary/USGS-01646500"
    )
    assert requests[0].url.params["mimeType"] == "text/csv"
    assert requests[0].url.params["api_key"] == "secret"


def test_successful_empty_metadata_is_not_an_error() -> None:
    empty_json = {"type": "FeatureCollection", "features": [], "links": []}
    empty_csv = (
        "monitoringLocationIdentifier,characteristicGroup,characteristic,"
        "characteristicUserSupplied,resultCount,activityCount,firstActivity,"
        "mostRecentActivity\n"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/summary/" in request.url.path:
            return httpx.Response(200, content=empty_csv, request=request)
        return httpx.Response(200, json=empty_json, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            )
            assert await adapter.get_parameter_codes((DISCHARGE,)) == ()
            assert await adapter.get_monitoring_locations(
                ("USGS-01646500",)
            ) == ()
            assert await adapter.get_time_series_metadata(
                ("USGS-01646500",),
                (DISCHARGE,),
            ) == ()
            assert await adapter.get_sample_periods("USGS-01646500") == ()

    run(exercise())


def test_invalid_schema_and_unsafe_next_link_are_rejected() -> None:
    responses = [
        {"type": "FeatureCollection", "features": [{}], "links": []},
        {
            "type": "FeatureCollection",
            "features": [],
            "links": [{"rel": "next", "href": "https://evil.test/data"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            )
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_parameter_codes((DISCHARGE,))
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_monitoring_locations(
                    ("USGS-01646500",)
                )

    run(exercise())


def test_page_limit_stops_unbounded_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [],
                "links": [
                    {
                        "rel": "next",
                        "href": (
                            f"{OGC_BASE}/collections/monitoring-locations/"
                            "items?offset=1"
                        ),
                    }
                ],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
                max_pages=1,
            )
            with pytest.raises(PortResponseLimitError):
                await adapter.get_monitoring_locations(
                    ("USGS-01646500",)
                )

    run(exercise())


def test_unsupported_api_version_is_distinct() -> None:
    with pytest.raises(UnsupportedPortVersionError):
        UsgsWaterDataAdapter(
            object(),
            api_version="v0",
            ogc_base_url=OGC_BASE,
            samples_base_url=SAMPLES_BASE,
        )


def test_production_sources_do_not_reference_legacy_water_services() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    legacy = "waterservices.usgs.gov"
    matches = [
        path
        for path in app_root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json"}
        and legacy in path.read_text(encoding="utf-8")
    ]
    assert matches == []
