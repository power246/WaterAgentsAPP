import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.domain import GeoPoint
from app.infrastructure.adapters import GoogleGeocodingAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortRateLimitError,
    PortTimeoutError,
)

FIXTURES = Path(__file__).with_name("fixtures")


def run(coroutine):
    return asyncio.run(coroutine)


def payload() -> dict[str, object]:
    return json.loads(
        (FIXTURES / "google_geocoding_ok.json").read_text(encoding="utf-8")
    )


def test_forward_and_reverse_return_typed_administrative_facts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload(), request=request)

    async def exercise() -> None:
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=100_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            adapter = GoogleGeocodingAdapter(http, api_key="secret")
            candidates = await adapter.resolve_text("Potomac")
            reverse = await adapter.reverse(
                GeoPoint(latitude=38.75, longitude=-77.25)
            )
            assert [item.administrative_location.subdivision_code for item in candidates] == [
                "US-DC",
                "US-VA",
            ]
            assert reverse is not None
            assert reverse.country_code == "US"
            assert reverse.provider_identity == "google-geocoding:v3:dc-potomac"

    run(exercise())
    assert requests[0].url.params["components"] == "country:US"
    assert requests[0].url.params["address"] == "Potomac"
    assert requests[1].url.params["latlng"] == "38.75,-77.25"


def test_forward_single_candidate_remains_a_single_typed_result() -> None:
    single = payload()
    single["results"] = single["results"][:1]

    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=single, request=request)
        )
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=100_000,
            retry_delay_seconds=0,
            transport=transport,
        ) as http:
            candidates = await GoogleGeocodingAdapter(
                http,
                api_key="secret",
            ).resolve_text("Potomac")
            assert len(candidates) == 1
            assert candidates[0].administrative_location.country_code == "US"

    run(exercise())


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        ("OVER_QUERY_LIMIT", PortRateLimitError),
        ("REQUEST_DENIED", PortAuthenticationError),
        ("INVALID_REQUEST", PortConfigurationError),
        ("UNRECOGNIZED", InvalidPortResponseError),
    ],
)
def test_google_status_taxonomy(status: str, error_type: type[Exception]) -> None:
    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": status, "results": []},
                request=request,
            )
        )
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=1000,
            retry_delay_seconds=0,
            transport=transport,
        ) as http:
            with pytest.raises(error_type):
                await GoogleGeocodingAdapter(
                    http,
                    api_key="secret",
                ).resolve_text("Potomac")

    run(exercise())


def test_zero_results_is_empty_and_malformed_result_is_invalid() -> None:
    responses = [
        {"status": "ZERO_RESULTS", "results": []},
        {"status": "OK", "results": [{}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0), request=request)

    async def exercise() -> None:
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=1000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            adapter = GoogleGeocodingAdapter(http, api_key="secret")
            assert await adapter.resolve_text("missing") == ()
            with pytest.raises(InvalidPortResponseError):
                await adapter.resolve_text("malformed")

    run(exercise())


def test_malformed_address_component_type_is_invalid() -> None:
    malformed = payload()
    malformed["results"][0]["address_components"][0]["types"] = [{}]

    async def exercise() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=malformed,
                request=request,
            )
        )
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=100_000,
            retry_delay_seconds=0,
            transport=transport,
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await GoogleGeocodingAdapter(
                    http,
                    api_key="secret",
                ).resolve_text("Potomac")

    run(exercise())


def test_timeout_retries_once_and_missing_key_fails_before_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture timeout", request=request)

    async def exercise() -> None:
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=1000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as http:
            with pytest.raises(PortConfigurationError):
                GoogleGeocodingAdapter(http, api_key="")
            with pytest.raises(PortTimeoutError):
                await GoogleGeocodingAdapter(
                    http,
                    api_key="secret",
                ).resolve_text("Potomac")

    run(exercise())
    assert calls == 2
