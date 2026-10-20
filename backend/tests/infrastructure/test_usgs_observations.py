import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from app.domain import (
    AvailabilityTimeRange,
    DischargeObservationRecord,
    ParameterKey,
    ParameterProvider,
    SamplePeriodOfRecord,
    TimeRange,
    WaterObservationRecord,
    WaterObservationRequest,
)
from app.infrastructure.adapters import UsgsObservationAdapter, UsgsWaterDataAdapter
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
SAMPLE_REF = (
    'usgs-samples:v1:["USGS-01646500","Physical",'
    '"Dissolved oxygen","Dissolved oxygen"]'
)
OTHER_SAMPLE_REF = (
    'usgs-samples:v1:["USGS-01646500","Physical",'
    '"Dissolved oxygen","DO field"]'
)


def run(coroutine):
    return asyncio.run(coroutine)


def json_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def bytes_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def sample_csv() -> bytes:
    return bytes_fixture("usgs_samples_narrow.csv").replace(
        b'"Oxygen, dissolved"', b'"Dissolved oxygen"'
    )


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
    series_refs: tuple[str, ...] | None = None,
    analysis_timezone: str = "UTC",
    request_range: TimeRange | None = None,
) -> WaterObservationRequest:
    return WaterObservationRequest(
        operation_id="m6-usgs-collection",
        provider=provider,
        channel=channel,
        station_id="USGS-01646500",
        series_refs=series_refs if series_refs is not None else (series_ref,),
        parameter_keys=(parameter,),
        request_range=request_range or TimeRange(start=START, end=END),
        analysis_timezone=analysis_timezone,
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
    assert requests[0].url.params["datetime"].endswith("2024-01-02T23:59:59.999999Z")
    assert list(requests[1].url.params.multi_items()) == [
        ("cursor", "official-page-2")
    ]
    assert all(item.headers["X-Api-Key"] == "m6-secret-key" for item in requests)
    assert all("m6-secret-key" not in str(item.url) for item in requests)


def test_continuous_query_excludes_the_result_window_end() -> None:
    window_end = datetime(2024, 1, 2, 6, tzinfo=UTC)
    query_ends: list[datetime] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        upper_text = http_request.url.params["datetime"].split("/", 1)[1]
        query_end = datetime.fromisoformat(upper_text.replace("Z", "+00:00"))
        query_ends.append(query_end)
        payload = json_fixture("usgs_continuous_page_1.json")
        inside = payload["features"][0]
        inside["properties"]["time"] = "2024-01-02T00:30:00Z"
        boundary = deepcopy(inside)
        boundary["id"] = "continuous-obs-at-end"
        boundary["properties"]["time"] = "2024-01-02T06:00:00Z"
        payload["features"] = [inside]
        if query_end >= window_end:
            payload["features"].append(boundary)
        payload["links"] = []
        return httpx.Response(200, json=payload, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(
                request(
                    request_range=TimeRange(
                        start=datetime(2024, 1, 2, tzinfo=UTC),
                        end=window_end,
                    )
                )
            )

    batch = run(exercise())
    assert query_ends == [window_end - timedelta(microseconds=1)]
    assert len(batch.records) == 1
    assert batch.records[0].observed_time == datetime(2024, 1, 2, 0, 30, tzinfo=UTC)


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


def test_null_continuous_measurement_is_skipped_and_next_page_is_collected() -> None:
    first = json_fixture("usgs_continuous_page_1.json")
    first["features"][0]["properties"]["value"] = None
    first["features"][0]["properties"]["qualifier"] = ["BACKWATER"]
    second = json_fixture("usgs_continuous_page_2.json")

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = second if http_request.url.params.get("cursor") else first
        return httpx.Response(200, json=payload, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(request())

    batch = run(exercise())
    assert [record.value for record in batch.records] == ["8.1"]
    assert [page.record_count for page in batch.page_or_chunk_manifests] == [0, 1]
    assert batch.counts.requests == 2
    assert batch.counts.records == 1
    assert batch.completion_proof.complete is True
    assert batch.completed_ranges == (TimeRange(start=START, end=END),)


def test_all_null_daily_measurements_are_complete_empty() -> None:
    payload = json_fixture("usgs_daily_discharge.json")
    payload["features"][0]["properties"]["value"] = None
    payload["features"][0]["properties"]["qualifier"] = ["BACKWATER"]

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(
                request(
                    channel="daily",
                    parameter=DISCHARGE,
                    series_ref="series-daily-00060",
                )
            )

    batch = run(exercise())
    assert batch.records == ()
    assert batch.page_or_chunk_manifests[0].record_count == 0
    assert batch.counts.records == 0
    assert batch.completion_proof.complete is True
    assert batch.completed_ranges == (TimeRange(start=START, end=END),)


@pytest.mark.parametrize("bad_value", [42, [], {"reading": "8.2"}])
def test_wrong_non_null_ogc_value_is_invalid(bad_value: object) -> None:
    payload = json_fixture("usgs_continuous_page_1.json")
    payload["features"][0]["properties"]["value"] = bad_value

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(request())

    with pytest.raises(InvalidPortResponseError):
        run(exercise())


@pytest.mark.parametrize("mutation", ["missing_value", "wrong_station", "wrong_time"])
def test_null_ogc_value_still_requires_value_key_and_matching_identity_time(
    mutation: str,
) -> None:
    payload = json_fixture("usgs_continuous_page_1.json")
    properties = payload["features"][0]["properties"]
    properties["value"] = None
    if mutation == "missing_value":
        del properties["value"]
    elif mutation == "wrong_station":
        properties["monitoring_location_id"] = "USGS-other"
    else:
        properties["time"] = "2023-12-31T12:00:00Z"

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(request())

    with pytest.raises(InvalidPortResponseError):
        run(exercise())


def test_null_ogc_features_count_toward_the_raw_record_limit() -> None:
    first = json_fixture("usgs_continuous_page_1.json")
    first["features"][0]["properties"]["value"] = None
    second = json_fixture("usgs_continuous_page_2.json")

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = second if http_request.url.params.get("cursor") else first
        return httpx.Response(200, json=payload, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http, page_size=1, max_records=1).collect(request())

    batch = run(exercise())
    assert batch.records == ()
    assert batch.counts.requests == 2
    assert [page.page_complete for page in batch.page_or_chunk_manifests] == [
        True,
        False,
    ]
    assert batch.completed_ranges == ()
    assert batch.completion_proof.complete is False
    assert batch.incomplete_reason == "evidence_provider_response_too_large"


@pytest.mark.parametrize("qualifier", [["Ice", "Backwater"], [], ["Ice", 5], [""]])
def test_ogc_qualifier_arrays_are_normalized_or_rejected(
    qualifier: list[object],
) -> None:
    payload = json_fixture("usgs_continuous_page_1.json")
    payload["features"][0]["properties"]["qualifier"] = qualifier
    payload["links"] = []

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(request())

    if any(not isinstance(item, str) or not item.strip() for item in qualifier):
        with pytest.raises(InvalidPortResponseError):
            run(exercise())
    else:
        batch = run(exercise())
        assert batch.records[0].provider_quality.qualifiers == tuple(qualifier)


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


@pytest.mark.parametrize(
    ("local_date", "expected_start", "expected_end"),
    [
        ("2024-03-10", "2024-03-10T06:00:00+00:00", "2024-03-11T05:00:00+00:00"),
        ("2024-11-03", "2024-11-03T05:00:00+00:00", "2024-11-04T06:00:00+00:00"),
    ],
)
def test_daily_date_covers_local_day_across_dst(
    local_date: str, expected_start: str, expected_end: str
) -> None:
    payload = json_fixture("usgs_daily_discharge.json")
    payload["features"][0]["properties"]["time"] = local_date
    start = datetime.fromisoformat(expected_start)
    end = datetime.fromisoformat(expected_end)

    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(
                request(
                    channel="daily",
                    parameter=DISCHARGE,
                    series_ref="series-daily-00060",
                    analysis_timezone="America/Chicago",
                    request_range=TimeRange(start=start, end=end),
                )
            )

    record = run(exercise()).records[0]
    assert record.observed_time is None
    assert record.valid_time_range == TimeRange(start=start, end=end)
    assert record.provenance.source_timezone is None


def test_daily_date_overlaps_window_only_when_its_local_day_intersects() -> None:
    payload = json_fixture("usgs_daily_discharge.json")
    payload["features"][0]["properties"]["time"] = "2026-10-11"

    async def exercise(window: TimeRange):
        async with provider_client(
            lambda http_request: httpx.Response(200, json=payload, request=http_request)
        ) as http:
            return await adapter(http).collect(
                request(
                    channel="daily",
                    parameter=DISCHARGE,
                    series_ref="series-daily-00060",
                    analysis_timezone="America/Chicago",
                    request_range=window,
                )
            )

    overlapping = TimeRange(
        start=datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc),
    )
    assert len(run(exercise(overlapping)).records) == 1
    outside = TimeRange(
        start=datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(InvalidPortResponseError):
        run(exercise(outside))


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
            content=sample_csv(),
            headers={"Content-Type": "text/csv"},
            request=http_request,
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http, api_key="samples-secret").collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref=SAMPLE_REF,
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
    assert http_request.url.params["characteristicGroup"] == "Physical"
    assert http_request.url.params["characteristic"] == "Dissolved oxygen"
    assert http_request.url.params["characteristicUserSupplied"] == "Dissolved oxygen"
    assert http_request.url.params["activityStartDateLower"] == "2023-12-31"
    assert http_request.url.params["activityStartDateUpper"] == "2024-01-03"
    assert http_request.headers["X-Api-Key"] == "samples-secret"
    assert "samples-secret" not in str(http_request.url)


def test_sample_qualified_by_local_date_is_collected_in_utc_window() -> None:
    calls: list[httpx.Request] = []
    header, original_row, _ = bytes_fixture("usgs_samples_narrow.csv").splitlines()
    row = original_row.replace(b",08:30:00,EST,", b",19:30:00,EST,")
    window = TimeRange(
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 2, 6, tzinfo=UTC),
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        lower = date.fromisoformat(http_request.url.params["activityStartDateLower"])
        upper = date.fromisoformat(http_request.url.params["activityStartDateUpper"])
        content = header + b"\n"
        if lower <= date(2024, 1, 1) <= upper:
            content += row + b"\n"
        return httpx.Response(200, content=content, request=http_request)

    async def exercise():
        async with provider_client(handler) as http:
            availability = UsgsWaterDataAdapter(http, samples_base_url=SAMPLES_BASE)
            period = SamplePeriodOfRecord(
                station_id="USGS-01646500",
                characteristic="Dissolved oxygen",
                first_activity=date(2024, 1, 1),
                most_recent_activity=date(2024, 1, 1),
                result_count=1,
                activity_count=1,
                provider_identity=(
                    'usgs-samples:v1:["USGS-01646500","Physical",'
                    '"Dissolved oxygen","Dissolved oxygen"]'
                ),
            )
            found = await availability.get_sample_observation_windows(
                period,
                WATER,
                (
                    AvailabilityTimeRange(
                        segment_index=0,
                        window_index=0,
                        start=window.start,
                        end=window.end,
                    ),
                ),
            )
            batch = await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref=period.provider_identity,
                    request_range=window,
                )
            )
            return found, batch

    found, batch = run(exercise())
    assert found == (0,)
    assert len(batch.records) == 1
    assert batch.records[0].observed_time == datetime(2024, 1, 2, 0, 30, tzinfo=UTC)
    assert len(calls) == 2
    assert all(
        call.url.params["activityStartDateLower"] == "2024-01-01"
        and call.url.params["activityStartDateUpper"] == "2024-01-03"
        and call.url.params["characteristicGroup"] == "Physical"
        and call.url.params["characteristic"] == "Dissolved oxygen"
        and call.url.params["characteristicUserSupplied"] == "Dissolved oxygen"
        for call in calls
    )


@pytest.mark.parametrize("second_repeats_first", [False, True])
def test_samples_collects_each_authorized_summary_identity_and_merges_complete_streams(
    second_repeats_first: bool,
) -> None:
    calls: list[httpx.Request] = []
    header, first, second = sample_csv().splitlines()

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        selected = (
            first
            if second_repeats_first
            or http_request.url.params["characteristicUserSupplied"]
            == "Dissolved oxygen"
            else second
        )
        return httpx.Response(
            200, content=header + b"\n" + selected + b"\n", request=http_request
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_refs=(SAMPLE_REF, OTHER_SAMPLE_REF),
                )
            )

    batch = run(exercise())
    assert batch.completion_proof.complete is True
    assert batch.completed_ranges == (TimeRange(start=START, end=END),)
    assert batch.counts.requests == 2
    assert batch.counts.pages == 2
    expected_count = 1 if second_repeats_first else 2
    assert batch.counts.records == expected_count
    assert [page.record_count for page in batch.page_or_chunk_manifests] == [
        1,
        0 if second_repeats_first else 1,
    ]
    assert len({record.record_id for record in batch.records}) == expected_count
    assert [call.url.params["characteristicUserSupplied"] for call in calls] == [
        "Dissolved oxygen",
        "DO field",
    ]
    assert all(
        call.url.params["characteristicGroup"] == "Physical"
        and call.url.params["characteristic"] == "Dissolved oxygen"
        for call in calls
    )


def test_samples_later_wrong_classification_keeps_only_partial_evidence() -> None:
    header, first, second = bytes_fixture("usgs_samples_narrow.csv").splitlines()

    def handler(http_request: httpx.Request) -> httpx.Response:
        selected = (
            first
            if http_request.url.params["characteristicUserSupplied"]
            == "Dissolved oxygen"
            else second
        )
        return httpx.Response(
            200, content=header + b"\n" + selected + b"\n", request=http_request
        )

    async def exercise():
        async with provider_client(handler) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_refs=(SAMPLE_REF, OTHER_SAMPLE_REF),
                )
            )

    batch = run(exercise())
    assert len(batch.records) == 1
    assert batch.counts.requests == 2
    assert batch.completed_ranges == ()
    assert batch.completion_proof.complete is False
    assert batch.incomplete_reason == "evidence_provider_response_invalid"
    assert [page.page_complete for page in batch.page_or_chunk_manifests] == [
        True,
        False,
    ]


def test_samples_wrong_classification_on_first_stream_is_invalid() -> None:
    async def exercise():
        async with provider_client(
            lambda http_request: httpx.Response(
                200, content=bytes_fixture("usgs_samples_narrow.csv"), request=http_request
            )
        ) as http:
            return await adapter(http).collect(
                request(
                    provider=ParameterProvider.USGS_SAMPLES,
                    channel="discrete_samples",
                    series_ref=SAMPLE_REF,
                )
            )

    with pytest.raises(InvalidPortResponseError):
        run(exercise())


def test_samples_identity_and_stream_limits_prevent_false_completion() -> None:
    header, first, second = sample_csv().splitlines()
    calls: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        selected = (
            first
            if http_request.url.params["characteristicUserSupplied"]
            == "Dissolved oxygen"
            else second
        )
        return httpx.Response(
            200, content=header + b"\n" + selected + b"\n", request=http_request
        )

    async def exercise():
        async with provider_client(handler) as http:
            two_refs = request(
                provider=ParameterProvider.USGS_SAMPLES,
                channel="discrete_samples",
                series_refs=(SAMPLE_REF, OTHER_SAMPLE_REF),
            )
            with pytest.raises(ValueError):
                await adapter(http).collect(
                    request(
                        provider=ParameterProvider.USGS_SAMPLES,
                        channel="discrete_samples",
                        series_ref="samples-summary-00300",
                    )
                )
            with pytest.raises(PortResponseLimitError):
                await adapter(http, max_pages=1).collect(two_refs)
            assert calls == []
            return await adapter(http, page_size=1, max_records=1).collect(two_refs)

    batch = run(exercise())
    assert len(batch.records) == 1
    assert batch.counts.requests == 2
    assert batch.completed_ranges == ()
    assert batch.completion_proof.complete is False
    assert batch.incomplete_reason == "evidence_provider_response_too_large"


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
                    series_ref=SAMPLE_REF,
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
                    series_ref=SAMPLE_REF,
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
                    series_ref=SAMPLE_REF,
                )
            )

    with pytest.raises(InvalidPortResponseError):
        run(exercise())


def test_samples_byte_and_record_limits_are_explicit() -> None:
    content = sample_csv()

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
                    series_ref=SAMPLE_REF,
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
                    series_ref=SAMPLE_REF,
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
                    series_ref=SAMPLE_REF,
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
