import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from app.domain import GeoPoint, TimeRange, WeatherEvidenceRequest
from app.infrastructure.adapters import NwsWeatherAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import InvalidPortResponseError

FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
WINDOW = TimeRange(
    start=datetime(2026, 9, 30, 9, tzinfo=timezone.utc),
    end=datetime(2026, 9, 30, 17, tzinfo=timezone.utc),
)


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


def weather_request(kind: str, variables: tuple[str, ...]) -> WeatherEvidenceRequest:
    return WeatherEvidenceRequest(
        operation_id=f"weather-{kind}",
        evidence_kind=kind,
        location=GeoPoint(latitude=38.9, longitude=-77.0),
        variables=variables,
        request_range=WINDOW,
        affected_window_ids=("w000",),
    )


def test_station_discovery_observations_pagination_missing_and_quality() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/points/"):
            payload = fixture("nws_point.json")
        elif request.url.path.endswith("/stations"):
            payload = fixture("nws_stations.json")
        elif request.url.params.get("cursor") == "next-page":
            payload = fixture("nws_observations_page_2.json")
        else:
            payload = fixture("nws_observations_page_1.json")
            payload["pagination"]["next"] = (
                f"{request.url}&cursor=next-page"
            )
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(
                weather_request(
                    "observation", ("temperature", "precipitationLastHour")
                )
            )
        assert batch.completion_proof.complete is True
        assert batch.counts.requests == 4
        assert len(batch.records) == 3
        assert [item.weather_variable for item in batch.records] == [
            "temperature",
            "temperature",
            "precipitationLastHour",
        ]
        precipitation = batch.records[-1]
        assert precipitation.duration_seconds == 3600
        assert precipitation.statistic == "accumulation"
        assert precipitation.raw_unit == "wmoUnit:mm"
        assert precipitation.provider_quality.qualifiers == ("quality_control:V",)
        assert batch.page_or_chunk_manifests[-1].next_page_identity is None

    run(exercise())
    assert all(request.url.scheme == "https" for request in requests)
    assert all(request.url.host == "api.weather.gov" for request in requests)
    assert all(
        request.headers["User-Agent"]
        == "WaterAgents (maintainer@example.test)"
        for request in requests
    )
    first_observation = requests[2]
    assert first_observation.url.params["start"].endswith("Z")
    assert first_observation.url.params["limit"] == "500"
    assert "secret" not in " ".join(str(request.url) for request in requests)


@pytest.mark.parametrize(
    ("max_pages", "expected_stations"),
    ((4, {"CLOSE", "MID"}), (3, {"CLOSE"})),
)
def test_bounded_station_sample_retains_records_without_claiming_full_discovery(
    max_pages: int, expected_stations: set[str]
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/points/"):
            payload = fixture("nws_point.json")
        elif request.url.path.endswith("/stations"):
            payload = fixture("nws_stations.json")
            payload["features"] = [
                {
                    "id": f"https://api.weather.gov/stations/{station_id}",
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [-77.0, latitude],
                    },
                    "properties": {"stationIdentifier": station_id},
                }
                for station_id, latitude in (
                    ("FAR", 38.0),
                    ("CLOSE", 38.89),
                    ("MID", 38.8),
                )
            ]
            payload["pagination"] = {
                "next": (
                    "https://api.weather.gov/stations"
                    "?id[0]=FAR&cursor=next-page"
                )
            }
        else:
            station_id = request.url.path.split("/")[2]
            payload = fixture("nws_observations_page_1.json")
            payload["features"][0]["id"] = (
                "https://api.weather.gov/stations/"
                f"{station_id}/observations/2026-09-30T10:00:00+00:00"
            )
            payload.pop("pagination")
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                max_stations=2,
                max_pages=max_pages,
                clock=lambda: NOW,
            ).collect(weather_request("observation", ("temperature",)))
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_response_too_large"
        assert batch.completed_ranges == ()
        assert batch.counts.requests == max_pages
        assert {item.location.station_id for item in batch.records} == expected_stations
        station_page = batch.page_or_chunk_manifests[1]
        assert all(
            item.provenance.discovery_identities == (station_page.page_identity,)
            for item in batch.records
        )

    run(exercise())
    assert requests[1].url.params["limit"] == "2"
    assert len(requests) == max_pages


def test_repeated_observation_next_returns_typed_incomplete_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/points/"):
            payload = fixture("nws_point.json")
        elif request.url.path.endswith("/stations"):
            payload = fixture("nws_stations.json")
        else:
            payload = fixture("nws_observations_page_1.json")
            payload["pagination"]["next"] = str(request.url)
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(weather_request("observation", ("temperature",)))
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_response_too_large"
        assert batch.counts.requests == 3
        assert batch.counts.records == 1
        assert batch.page_or_chunk_manifests[-1].page_complete is False

    run(exercise())


def test_cursor_only_observation_next_keeps_bounded_record_and_stops() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/points/"):
            payload = fixture("nws_point.json")
        elif request.url.path.endswith("/stations"):
            payload = fixture("nws_stations.json")
        else:
            payload = fixture("nws_observations_page_1.json")
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(weather_request("observation", ("temperature",)))
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_response_invalid"
        assert batch.counts.requests == 3
        assert batch.counts.records == 1

    run(exercise())
    assert len(requests) == 3


def test_grid_forecast_preserves_issue_valid_interval_unit_and_duration() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = (
            fixture("nws_point.json")
            if request.url.path.startswith("/points/")
            else fixture("nws_grid_forecast.json")
        )
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(
                weather_request(
                    "forecast", ("temperature", "quantitativePrecipitation")
                )
            )
        assert len(batch.records) == 2
        temperature, precipitation = batch.records
        assert temperature.grid_id == "LWX/96,70"
        assert temperature.issued_at.isoformat() == "2026-09-30T09:30:00+00:00"
        assert temperature.valid_time_range.end.isoformat() == (
            "2026-09-30T11:00:00+00:00"
        )
        assert temperature.duration_seconds == 3600
        assert temperature.raw_unit == "wmoUnit:degC"
        assert precipitation.duration_seconds == 21600
        assert all(item.statistic == "forecast_value" for item in batch.records)

    run(exercise())
    assert requests[1].url.path == "/gridpoints/LWX/96,70"


def test_point_discovery_uses_nws_canonical_coordinate_precision() -> None:
    requests: list[httpx.Request] = []
    canonical_path = "/points/46.7096,-92.4188"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/points/"):
            if request.url.path != canonical_path:
                return httpx.Response(
                    301,
                    headers={"Location": f"https://api.weather.gov{canonical_path}"},
                    request=request,
                )
            payload = fixture("nws_point.json")
        else:
            payload = fixture("nws_grid_forecast.json")
        return httpx.Response(200, json=payload, request=request)

    request = weather_request("forecast", ("temperature",)).model_copy(
        update={
            "location": GeoPoint(
                latitude=46.709564810393,
                longitude=-92.418807116152,
            )
        }
    )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(request)
        assert batch.completion_proof.complete is True

    run(exercise())
    assert requests[0].url.path == canonical_path


def test_cross_origin_discovery_link_is_never_requested() -> None:
    requests: list[httpx.Request] = []
    point = fixture("nws_point.json")
    point["properties"]["forecastGridData"] = "https://evil.example/collect"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=point, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            )
            with pytest.raises(InvalidPortResponseError):
                await adapter.collect(weather_request("forecast", ("temperature",)))

    run(exercise())
    assert len(requests) == 1


def test_malformed_grid_value_is_typed_incomplete_not_empty_success() -> None:
    malformed = fixture("nws_grid_forecast.json")
    malformed["properties"]["temperature"]["values"][0]["value"] = "hot"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            fixture("nws_point.json")
            if request.url.path.startswith("/points/")
            else malformed
        )
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(weather_request("forecast", ("temperature",)))
        assert batch.completion_proof.complete is False
        assert batch.incomplete_reason == "evidence_provider_response_invalid"
        assert batch.records == ()
        assert batch.page_or_chunk_manifests[-1].page_complete is False

    run(exercise())


def test_nws_configuration_rejects_nonofficial_origin_and_header_injection() -> None:
    client = provider_client(
        lambda request: httpx.Response(200, json={}, request=request)
    )
    for values in (
        {"base_url": "https://evil.example"},
        {"user_agent_contact": "safe@example.test\r\nX-Secret: value"},
    ):
        kwargs = {
            "user_agent_project": "WaterAgents",
            "user_agent_contact": "maintainer@example.test",
            **values,
        }
        try:
            NwsWeatherAdapter(client, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe NWS configuration was accepted")
