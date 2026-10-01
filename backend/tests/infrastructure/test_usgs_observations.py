import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.domain import (
    DischargeObservationRecord,
    ParameterKey,
    ParameterProvider,
    TimeRange,
    WaterObservationRecord,
    WaterObservationRequest,
)
from app.infrastructure.adapters import UsgsObservationAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortResponseLimitError,
    PortUnavailableError,
)

FIXTURES = Path(__file__).with_name("fixtures")
OGC_BASE = "https://provider.test/ogcapi/v1"
SAMPLES_BASE = "https://provider.test/samples-data"
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 3, tzinfo=timezone.utc)
NOW = datetime(2024, 1, 4, tzinfo=timezone.utc)
WATER = ParameterKey(code_system="usgs", code="00300")
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
OTHER_DISCHARGE = ParameterKey(code_system="usgs", code="80155")


def run(coroutine):
    return asyncio.run(coroutine)


def json_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def bytes_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def provider_client(
    handler,
    *,
    max_response_bytes: int = 200_000,
) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=max_response_bytes,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def request(
    *,
    provider: ParameterProvider = ParameterProvider.WDFN_TIME_SERIES,
    channel: str = "continuous",
    parameter: ParameterKey = WATER,
    series_ref: str = "series-continuous-00300",
) -> WaterObservationRequest:
    return WaterObservationRequest(
        operation_id="m6-usgs-collection",
        provider=provider,
        channel=channel,
        station_id="USGS-01646500",
        series_refs=(series_ref,),
        parameter_keys=(parameter,),
        request_range=TimeRange(start=START, end=END),
        affected_window_ids=("window-1",),
    )


def adapter(http: ProviderHttpClient, **kwargs) -> UsgsObservationAdapter:
    return UsgsObservationAdapter(
        http,
        ogc_base_url=OGC_BASE,
        samples_base_url=SAMPLES_BASE,
        clock=lambda: NOW,
        **kwargs,
    )


def test_continuous_follows_only_official_next_and_preserves_contract_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request)
        fixture = (
            "usgs_continuous_page_2.json"
            if http_request.url.params.get("cursor")
            else "usgs_continuous_page_1.json"
        )
        return httpx.Response(
            200, json=json_fixture(fixture), request=http_request
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http, api_key="m6-secret-key", page_size=100).collect(
                request()
            )

    batch = run(exercise())
    assert batch.completion_proof.complete is True
    assert batch.completion_proof.contract.value == "official_next_chain"
    assert batch.counts.requests == 2
    assert batch.counts.pages == 2
    assert batch.counts.records == 2
    assert batch.completed_ranges == (TimeRange(start=START, end=END),)
    assert batch.page_or_chunk_manifests[0].next_page_identity == (
        batch.page_or_chunk_manifests[1].page_identity
    )
    assert [record.value for record in batch.records] == ["8.2", "8.1"]
    assert [record.provider_quality.approval for record in batch.records] == [
        "Provisional",
        "Approved",
    ]
    assert batch.records[0].provider_quality.qualifiers == ("Ice",)
    assert all(record.parameter == WATER for record in batch.records)
    assert all(record.statistic == "00000" for record in batch.records)
    assert all(record.raw_unit == "mg/L" for record in batch.records)
    assert all("?" not in record.source_uri for record in batch.records)
    assert len(requests) == 2  # a short page did not guess completion
    assert requests[0].url.params["monitoring_location_id"] == "USGS-01646500"
    assert requests[0].url.params["time_series_id"] == "series-continuous-00300"
    assert requests[0].url.params["parameter_code"] == "00300"
    assert requests[0].url.params["datetime"].endswith("2024-01-03T00:00:00Z")
    assert list(requests[1].url.params.multi_items()) == [
        ("cursor", "official-page-2")
    ]
    assert all(item.headers["X-Api-Key"] == "m6-secret-key" for item in requests)
    assert all("m6-secret-key" not in str(item.url) for item in requests)


def test_successful_empty_ogc_is_complete_empty_not_unavailable() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": [], "links": []},
            request=http_request,
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(request())

    batch = run(exercise())
    assert batch.records == ()
    assert batch.counts.records == 0
    assert batch.completion_proof.complete is True
    assert batch.completed_ranges == (TimeRange(start=START, end=END),)


def test_daily_discharge_preserves_provider_parameter_time_and_statistic() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=json_fixture("usgs_daily_discharge.json"),
            request=http_request,
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(
                request(
                    channel="daily",
                    parameter=DISCHARGE,
                    series_ref="series-daily-00060",
                )
            )

    batch = run(exercise())
    record = batch.records[0]
    assert isinstance(record, DischargeObservationRecord)
    assert record.parameter == DISCHARGE
    assert record.statistic == "00003"
    assert record.value == "579.4"
    assert record.observed_time == START
    assert record.provider_quality.qualifiers == ("Working",)


def test_registered_non_00060_discharge_is_not_renamed_or_estimated() -> None:
    payload = json_fixture("usgs_daily_discharge.json")
    properties = payload["features"][0]["properties"]
    properties["parameter_code"] = "80155"
    properties["time_series_id"] = "series-daily-80155"
    properties["statistic_id"] = "00001"

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(
                http,
                discharge_parameters=(DISCHARGE, OTHER_DISCHARGE),
            ).collect(
                request(
                    channel="daily",
                    parameter=OTHER_DISCHARGE,
                    series_ref="series-daily-80155",
                )
            )

    record = run(exercise()).records[0]
    assert isinstance(record, DischargeObservationRecord)
    assert record.parameter == OTHER_DISCHARGE
    assert record.statistic == "00001"
    assert record.method is None


def test_identical_duplicate_identity_is_deduplicated_but_conflict_is_invalid() -> None:
    first = json_fixture("usgs_continuous_page_1.json")
    duplicate = deepcopy(first)
    duplicate["links"] = []

    def duplicate_handler(http_request: httpx.Request) -> httpx.Response:
        payload = duplicate if http_request.url.params.get("cursor") else first
        return httpx.Response(200, json=payload, request=http_request)

    async def collect_duplicate():
        async with provider_client(duplicate_handler) as http:
            return await adapter(http).collect(request())

    batch = run(collect_duplicate())
    assert len(batch.records) == 1
    assert [page.record_count for page in batch.page_or_chunk_manifests] == [1, 0]

    conflict = deepcopy(duplicate)
    conflict["features"][0]["properties"]["value"] = "9.9"

    def conflict_handler(http_request: httpx.Request) -> httpx.Response:
        payload = conflict if http_request.url.params.get("cursor") else first
        return httpx.Response(200, json=payload, request=http_request)

    async def collect_conflict():
        async with provider_client(conflict_handler) as http:
            return await adapter(http).collect(request())

    incomplete = run(collect_conflict())
    assert len(incomplete.records) == 1
    assert incomplete.completion_proof.complete is False
    assert incomplete.incomplete_reason == "evidence_provider_response_invalid"
    assert incomplete.page_or_chunk_manifests[-1].page_complete is False


@pytest.mark.parametrize(
    ("failure", "reason", "expected_requests"),
    [
        ("invalid", "evidence_provider_response_invalid", 2),
        ("rate", "evidence_provider_rate_limited", 3),
        ("timeout", "evidence_provider_timeout", 3),
    ],
)
def test_page_two_failure_retains_page_one_without_guessing_completed_range(
    failure: str,
    reason: str,
    expected_requests: int,
) -> None:
    first = json_fixture("usgs_continuous_page_1.json")

    def handler(http_request: httpx.Request) -> httpx.Response:
        if not http_request.url.params.get("cursor"):
            return httpx.Response(200, json=first, request=http_request)
        if failure == "invalid":
            return httpx.Response(200, content=b"{", request=http_request)
        if failure == "rate":
            return httpx.Response(429, request=http_request)
        raise httpx.ReadTimeout("fixture timeout", request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(request())

    batch = run(exercise())
    assert len(batch.records) == 1
    assert batch.counts.requests == expected_requests
    assert batch.counts.pages == 2
    assert batch.completed_ranges == ()
    assert batch.completion_proof.complete is False
    assert batch.incomplete_reason == reason
    assert batch.page_or_chunk_manifests[0].page_complete is True
    assert batch.page_or_chunk_manifests[1].page_complete is False


def test_unsafe_or_multiple_next_links_and_malformed_feature_are_invalid() -> None:
    base = json_fixture("usgs_continuous_page_1.json")
    unsafe = deepcopy(base)
    unsafe["links"][0]["href"] = "http://provider.test/ogcapi/v1/collections/continuous/items?cursor=2"
    multiple = deepcopy(base)
    multiple["links"].append(deepcopy(multiple["links"][0]))
    malformed = deepcopy(base)
    del malformed["features"][0]["properties"]["unit_of_measure"]

    for payload in (unsafe, multiple, malformed):
        async def exercise(payload=payload):
            async with provider_client(
                lambda http_request: httpx.Response(
                    200, json=payload, request=http_request
                )
            ) as http:
                return await adapter(http).collect(request())

        with pytest.raises(InvalidPortResponseError):
            run(exercise())


def test_ogc_page_and_record_limits_do_not_claim_complete_collection() -> None:
    first = json_fixture("usgs_continuous_page_1.json")

    async def page_limit():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, json=first, request=http_request
            )
        ) as http:
            return await adapter(http, page_size=1, max_pages=1).collect(request())

    incomplete = run(page_limit())
    assert len(incomplete.records) == 1
    assert incomplete.completion_proof.complete is False
    assert incomplete.completed_ranges == ()
    assert incomplete.incomplete_reason == "evidence_provider_response_too_large"

    too_many = deepcopy(first)
    too_many["links"] = []
    second = deepcopy(too_many["features"][0])
    second["id"] = "continuous-obs-extra"
    second["properties"]["time"] = "2024-01-01T12:05:00Z"
    too_many["features"].append(second)

    async def record_limit():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, json=too_many, request=http_request
            )
        ) as http:
            return await adapter(http, page_size=1, max_records=1).collect(request())

    with pytest.raises(PortResponseLimitError):
        run(record_limit())


def test_samples_csv_preserves_quoted_row_unknown_column_and_detection_qualifiers() -> None:
    requests: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request)
        return httpx.Response(
            200,
            content=bytes_fixture("usgs_samples_narrow.csv"),
            headers={"Content-Type": "text/csv"},
            request=http_request,
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http, api_key="samples-secret").collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    batch = run(exercise())
    assert batch.completion_proof.complete is True
    assert batch.completion_proof.contract.value == "complete_csv_stream"
    assert len(batch.records) == 2
    assert all(isinstance(item, WaterObservationRecord) for item in batch.records)
    assert batch.records[0].value == "8.2"
    assert batch.records[0].method == "Field meter"
    assert batch.records[0].statistic == "sample"
    assert batch.records[1].value == "0.05"
    assert batch.records[1].method == "Laboratory method"
    assert batch.records[1].provider_quality.qualifiers == (
        "<",
        "Not Detected",
        "Actual",
    )
    http_request = requests[0]
    assert http_request.url.path.endswith("/samples-data/results/narrow")
    assert http_request.url.params["mimeType"] == "text/csv"
    assert http_request.url.params["activityMediaName"] == "Water"
    assert http_request.url.params["monitoringLocationIdentifier"] == "USGS-01646500"
    assert http_request.url.params["usgsPCode"] == "00300"
    assert http_request.url.params["activityStartDateLower"] == "2024-01-01"
    assert http_request.url.params["activityStartDateUpper"] == "2024-01-02"
    assert http_request.headers["X-Api-Key"] == "samples-secret"
    assert "samples-secret" not in str(http_request.url)


def test_samples_successful_empty_csv_is_complete() -> None:
    header = bytes_fixture("usgs_samples_narrow.csv").splitlines(keepends=True)[0]

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=header, request=http_request
            )
        ) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    batch = run(exercise())
    assert batch.records == ()
    assert batch.completion_proof.complete is True


def test_samples_date_without_time_is_preserved_as_a_day_range() -> None:
    lines = bytes_fixture("usgs_samples_narrow.csv").splitlines(keepends=True)[:2]
    content = b"".join(lines).replace(b",08:30:00,EST,", b",,,", 1)

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=content, request=http_request
            )
        ) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    record = run(exercise()).records[0]
    assert record.observed_time is None
    assert record.valid_time_range == TimeRange(
        start=START,
        end=START + timedelta(days=1),
    )
    assert record.provenance.source_timezone is None


@pytest.mark.parametrize("mutation", ["missing_column", "invalid_number"])
def test_samples_missing_required_column_or_invalid_row_is_invalid(
    mutation: str,
) -> None:
    content = bytes_fixture("usgs_samples_narrow.csv")
    if mutation == "missing_column":
        content = content.replace(b"Result_MeasureUnit", b"Unknown_MeasureUnit", 1)
    else:
        content = content.replace(b",8.20,mg/L,", b",not-a-number,mg/L,", 1)

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=content, request=http_request
            )
        ) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    with pytest.raises(InvalidPortResponseError):
        run(exercise())


def test_samples_byte_and_record_limits_are_explicit() -> None:
    content = bytes_fixture("usgs_samples_narrow.csv")

    async def byte_limit():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=content, request=http_request
            ),
            max_response_bytes=64,
        ) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    with pytest.raises(PortResponseLimitError):
        run(byte_limit())

    async def record_limit():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=content, request=http_request
            )
        ) as http:
            return await adapter(http, page_size=1, max_records=1).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    with pytest.raises(PortResponseLimitError):
        run(record_limit())


def test_samples_stream_failure_retries_once_then_remains_technical_failure() -> None:
    calls = 0

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"Org_Identifier,Location_Identifier\n"
            raise httpx.RemoteProtocolError("fixture stream failed")

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=BrokenStream(), request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref="samples-summary-00300",
                )
            )

    with pytest.raises(PortUnavailableError):
        run(exercise())
    assert calls == 2


def test_secret_is_absent_from_errors_logs_records_and_completion_projection(
    caplog,
) -> None:
    secret = "m6-never-expose-this"
    first = json_fixture("usgs_continuous_page_1.json")
    requests: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request)
        if http_request.url.params.get("cursor"):
            return httpx.Response(500, content=secret.encode(), request=http_request)
        return httpx.Response(200, json=first, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http, api_key=secret).collect(request())

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        batch = run(exercise())
    projection = batch.model_dump_json()
    assert batch.completion_proof.complete is False
    assert secret not in projection
    assert secret not in caplog.text
    assert all(secret not in str(item.url) for item in requests)
    assert all("?" not in record.source_uri for record in batch.records)
