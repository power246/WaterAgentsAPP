import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from app.domain import (
    AvailabilityTimeRange,
    ParameterKey,
    SamplePeriodOfRecord,
    TimeSeriesKind,
    TimeSeriesMetadata,
)
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
OXYGEN = ParameterKey(code_system="usgs", code="00300")


def probe_range(index: int, start: datetime, end: datetime) -> AvailabilityTimeRange:
    return AvailabilityTimeRange(
        segment_index=0, window_index=index, start=start, end=end
    )


def probe_window(index: int, start_hour: int, end_hour: int) -> AvailabilityTimeRange:
    day = datetime(2024, 1, 2, tzinfo=UTC)
    return probe_range(
        index, day + timedelta(hours=start_hour), day + timedelta(hours=end_hour)
    )


def observation_series(*, kind: TimeSeriesKind = TimeSeriesKind.CONTINUOUS) -> TimeSeriesMetadata:
    return TimeSeriesMetadata(
        series_id="series-1",
        station_id="USGS-01646500",
        parameter=OXYGEN,
        unit="mg/L",
        begin=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 1, tzinfo=UTC),
        kind=kind,
        provider_identity="usgs-wdfn:v1:time-series:series-1",
    )


def sample_period() -> SamplePeriodOfRecord:
    return SamplePeriodOfRecord(
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
    assert all(request.headers["X-Api-Key"] == "secret" for request in requests)
    assert all("api_key" not in request.url.params for request in requests)
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


@pytest.mark.parametrize(
    ("concept", "query_name", "query_value"),
    [
        ("00060", "id", "00060"),
        ("Discharge", "parameter_name", "Discharge"),
    ],
)
def test_parameter_catalog_candidates_use_exact_official_queryables(
    concept: str,
    query_name: str,
    query_value: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=json_fixture("wdfn_parameter_codes.json"),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            candidates = await UsgsWaterDataAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            ).find_candidates(concept)
        assert len(candidates) == 1
        assert candidates[0].key == DISCHARGE
        assert candidates[0].canonical_name == "Discharge"
        assert candidates[0].score == 1.0

    run(exercise())
    assert requests[0].url.path.endswith("/collections/parameter-codes/items")
    assert requests[0].url.params[query_name] == query_value


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
    assert requests[0].headers["X-Api-Key"] == "secret"
    assert "api_key" not in requests[0].url.params


def test_samples_same_characteristic_keeps_distinct_provider_identities() -> None:
    content = (
        "monitoringLocationIdentifier,characteristicGroup,characteristic,"
        "characteristicUserSupplied,resultCount,activityCount,firstActivity,"
        "mostRecentActivity\n"
        'USGS-04024000,Physical,pH,"pH, water, unfiltered, field",'
        "496,496,1955-09-18,2026-09-21\n"
        'USGS-04024000,Physical,pH,"pH, water, unfiltered, laboratory",'
        "99,99,1980-11-03,2015-09-01\n"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            periods = await UsgsWaterDataAdapter(
                http,
                ogc_base_url=OGC_BASE,
                samples_base_url=SAMPLES_BASE,
            ).get_sample_periods("USGS-04024000")
        assert len(periods) == 2
        assert [period.characteristic for period in periods] == ["pH", "pH"]
        assert periods[0].provider_identity != periods[1].provider_identity

    run(exercise())


def test_time_series_probe_uses_one_matching_observation_even_when_metadata_lags() -> (
    None
):
    requests: list[httpx.Request] = []
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "time_series_id": "series-1",
                    "monitoring_location_id": "USGS-01646500",
                    "parameter_code": "00300",
                    "time": "2024-01-02T12:30:00Z",
                    "value": "8.20",
                },
            }
        ],
        "links": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            assert await adapter.get_time_series_observation_windows(
                observation_series(),
                (probe_window(0, 12, 13),),
            ) == (0,)

    run(exercise())
    assert requests[0].url.path.endswith("/collections/continuous/items")
    assert requests[0].url.params["time_series_id"] == "series-1"
    assert requests[0].url.params["monitoring_location_id"] == "USGS-01646500"
    assert requests[0].url.params["parameter_code"] == "00300"
    assert requests[0].url.params["limit"] == "1000"
    assert requests[0].url.params["datetime"].startswith("2024-01-02T12:00:00Z/")


@pytest.mark.parametrize(
    ("kind", "observed", "requested"),
    [
        (
            TimeSeriesKind.CONTINUOUS,
            "2024-01-02T12:30:00Z",
            (probe_window(0, 12, 13),),
        ),
        (
            TimeSeriesKind.DAILY,
            "2024-01-02",
            (
                probe_range(
                    0,
                    datetime(2024, 1, 1, tzinfo=UTC),
                    datetime(2024, 1, 4, tzinfo=UTC),
                ),
            ),
        ),
    ],
)
def test_time_series_probe_null_value_does_not_qualify_window(
    kind: TimeSeriesKind,
    observed: str,
    requested: tuple[AvailabilityTimeRange, ...],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": observed,
                            "value": None,
                        },
                    }
                ],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            assert (
                await adapter.get_time_series_observation_windows(
                    observation_series(kind=kind), requested
                )
                == ()
            )

    run(exercise())


def test_time_series_probe_null_page_does_not_hide_later_measurement() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        second = request.url.params.get("offset") == "1"
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": (
                                "2024-01-02T06:30:00Z"
                                if second
                                else "2024-01-02T00:30:00Z"
                            ),
                            "value": "8.20" if second else None,
                        },
                    }
                ],
                "links": []
                if second
                else [
                    {
                        "rel": "next",
                        "href": f"{OGC_BASE}/collections/continuous/items?offset=1",
                    }
                ],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http, ogc_base_url=OGC_BASE, page_size=1, max_pages=2
            )
            assert await adapter.get_time_series_observation_windows(
                observation_series(),
                (probe_window(0, 0, 6), probe_window(1, 6, 12)),
            ) == (1,)

    run(exercise())
    assert len(requests) == 2


def test_time_series_probe_empty_and_incomplete_pages_are_distinct() -> None:
    pages = [
        {"type": "FeatureCollection", "features": [], "links": []},
        {
            "type": "FeatureCollection",
            "features": [],
            "links": [
                {
                    "rel": "next",
                    "href": f"{OGC_BASE}/collections/continuous/items?offset=1",
                }
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0), request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            requested = (probe_window(0, 0, 24),)
            assert (
                await adapter.get_time_series_observation_windows(
                    observation_series(), requested
                )
                == ()
            )
            with pytest.raises(PortResponseLimitError):
                await adapter.get_time_series_observation_windows(
                    observation_series(), requested
                )

    run(exercise())


@pytest.mark.parametrize(
    "field", ["time_series_id", "monitoring_location_id", "parameter_code", "time"]
)
@pytest.mark.parametrize("value", ["8.20", None])
def test_time_series_probe_rejects_mismatched_observation(
    field: str, value: str | None
) -> None:
    properties = {
        "time_series_id": "series-1",
        "monitoring_location_id": "USGS-01646500",
        "parameter_code": "00300",
        "time": "2024-01-02T12:30:00Z",
        "value": value,
    }
    properties[field] = "2024-01-03T00:00:00Z" if field == "time" else "wrong"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": properties}],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_time_series_observation_windows(
                    observation_series(),
                    (probe_window(0, 12, 13),),
                )

    run(exercise())


def test_time_series_probe_rejects_missing_value_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": "2024-01-02T12:30:00Z",
                        },
                    }
                ],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_time_series_observation_windows(
                    observation_series(), (probe_window(0, 12, 13),)
                )

    run(exercise())


def test_time_series_probe_logs_malformed_record_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = {
        "time_series_id": "series-1",
        "monitoring_location_id": "USGS-01646500",
        "parameter_code": "00300",
        "time": "2024-01-02T00:30:00Z",
        "value": "8.20",
    }
    malformed = {
        **first,
        "time": "2024-01-02T06:30:00Z",
        "value": 8.2,
        "unrelated_provider_payload": "private-response-marker",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": first},
                    {"type": "Feature", "properties": malformed},
                ],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            with pytest.raises(InvalidPortResponseError) as raised:
                await adapter.get_time_series_observation_windows(
                    observation_series(),
                    (probe_window(0, 0, 6), probe_window(1, 6, 12)),
                )
            assert raised.value.provider == "usgs_waterdata"
            assert raised.value.operation == "get_time_series_observation_windows"

    with caplog.at_level(logging.WARNING, logger="wateragents.usgs_waterdata"):
        run(exercise())
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        == "usgs_observation_probe_validation_failed"
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert (
        failure.station_id,
        failure.series_id,
        failure.parameter_code,
        failure.page_number,
        failure.record_index,
        failure.validation_step,
        failure.provider_error,
    ) == (
        "USGS-01646500",
        "series-1",
        "00300",
        1,
        1,
        "value",
        "InvalidPortResponseError",
    )
    assert "private-response-marker" not in failure.getMessage()


def test_time_series_probe_logs_invalid_json_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json", request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_time_series_observation_windows(
                    observation_series(), (probe_window(0, 0, 6),)
                )

    with caplog.at_level(logging.WARNING, logger="wateragents.usgs_waterdata"):
        run(exercise())
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        == "usgs_observation_probe_validation_failed"
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.station_id == "USGS-01646500"
    assert failure.series_id == "series-1"
    assert failure.page_number == 1
    assert failure.record_index is None
    assert failure.validation_step == "json"


def test_daily_probe_keeps_date_only_value_out_of_six_hour_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": "2024-01-02",
                            "value": "8.20",
                        },
                    }
                ],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            assert (
                await adapter.get_time_series_observation_windows(
                    observation_series(kind=TimeSeriesKind.DAILY),
                    (probe_window(0, 12, 18),),
                )
                == ()
            )

    run(exercise())
    assert requests[0].url.path.endswith("/collections/daily/items")
    assert requests[0].url.params["datetime"] == "2024-01-01/2024-01-03"


def test_batch_time_series_probe_maps_boundary_values_after_pagination() -> None:
    requests: list[httpx.Request] = []

    def feature(observed: str) -> dict:
        return {
            "type": "Feature",
            "properties": {
                "time_series_id": "series-1",
                "monitoring_location_id": "USGS-01646500",
                "parameter_code": "00300",
                "time": observed,
                "value": "8.20",
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        is_second = request.url.params.get("offset") == "1"
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    feature(
                        "2024-01-02T12:00:00Z" if is_second else "2024-01-02T06:00:00Z"
                    )
                ],
                "links": []
                if is_second
                else [
                    {
                        "rel": "next",
                        "href": f"{OGC_BASE}/collections/continuous/items?offset=1",
                    }
                ],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http, ogc_base_url=OGC_BASE, page_size=1, max_pages=2
            )
            assert await adapter.get_time_series_observation_windows(
                observation_series(),
                (
                    probe_window(0, 0, 6),
                    probe_window(1, 6, 12),
                    probe_window(2, 12, 18),
                ),
            ) == (1, 2)

    run(exercise())
    assert len(requests) == 2
    assert requests[0].url.params["limit"] == "1"
    assert requests[0].url.params["datetime"].startswith("2024-01-02T00:00:00Z/")


def test_batch_time_series_probe_does_not_call_incomplete_scan_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": "2024-01-02T00:30:00Z",
                            "value": "8.20",
                        },
                    }
                ],
                "links": [
                    {
                        "rel": "next",
                        "href": f"{OGC_BASE}/collections/continuous/items?offset=1",
                    }
                ],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http, ogc_base_url=OGC_BASE, page_size=1, max_pages=1
            )
            assert await adapter.get_time_series_observation_windows(
                observation_series(),
                (probe_window(0, 0, 6),),
            ) == (0,)
            with pytest.raises(PortResponseLimitError):
                await adapter.get_time_series_observation_windows(
                    observation_series(),
                    (probe_window(0, 0, 6), probe_window(1, 6, 12)),
                )

    run(exercise())


def test_batch_continuous_probe_rejects_query_beyond_provider_span() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("oversized USGS probe must not make an HTTP request")

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            with pytest.raises(PortResponseLimitError):
                await adapter.get_time_series_observation_windows(
                    observation_series(),
                    (
                        probe_range(
                            0,
                            datetime(2020, 1, 1, tzinfo=UTC),
                            datetime(2024, 1, 1, tzinfo=UTC),
                        ),
                    ),
                )

    run(exercise())


def test_batch_daily_probe_does_not_assign_date_only_value_to_utc_day() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "time_series_id": "series-1",
                            "monitoring_location_id": "USGS-01646500",
                            "parameter_code": "00300",
                            "time": "2024-01-02",
                            "value": "8.20",
                        },
                    }
                ],
                "links": [],
            },
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, ogc_base_url=OGC_BASE)
            assert (
                await adapter.get_time_series_observation_windows(
                    observation_series(kind=TimeSeriesKind.DAILY),
                    (probe_window(0, 0, 24),),
                )
                == ()
            )

    run(exercise())


def test_samples_probe_filters_exact_summary_identity_and_checks_sample_time() -> None:
    requests: list[httpx.Request] = []
    header, row, _ = (
        (FIXTURES / "usgs_samples_narrow.csv").read_text(encoding="utf-8").splitlines()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=f"{header}\n{row}\n".encode(), request=request
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, samples_base_url=SAMPLES_BASE)
            assert await adapter.get_sample_observation_windows(
                sample_period(),
                OXYGEN,
                (
                    probe_range(
                        0,
                        datetime(2024, 1, 1, 13, tzinfo=UTC),
                        datetime(2024, 1, 1, 14, tzinfo=UTC),
                    ),
                ),
            ) == (0,)
            assert (
                await adapter.get_sample_observation_windows(
                    sample_period(),
                    OXYGEN,
                    (
                        probe_range(
                            1,
                            datetime(2024, 1, 1, 14, tzinfo=UTC),
                            datetime(2024, 1, 1, 15, tzinfo=UTC),
                        ),
                    ),
                )
                == ()
            )

    run(exercise())
    assert all(request.url.path.endswith("/results/narrow") for request in requests)
    assert requests[0].url.params["characteristicGroup"] == "Physical"
    assert requests[0].url.params["characteristic"] == "Dissolved oxygen"
    assert requests[0].url.params["characteristicUserSupplied"] == "Dissolved oxygen"
    assert requests[0].url.params["usgsPCode"] == "00300"
    assert requests[0].url.params["activityStartDateLower"] == "2023-12-31"


def test_batch_samples_probe_expands_local_date_and_maps_exact_utc_window() -> None:
    requests: list[httpx.Request] = []
    header, row, _ = (
        (FIXTURES / "usgs_samples_narrow.csv").read_text(encoding="utf-8").splitlines()
    )
    timed_row = row.replace("08:30:00", "20:30:00")
    date_only_row = row.replace("2024-01-01,08:30:00,EST", "2024-01-02,,")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=f"{header}\n{timed_row}\n{date_only_row}\n".encode(),
            request=request,
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, samples_base_url=SAMPLES_BASE)
            assert await adapter.get_sample_observation_windows(
                sample_period(),
                OXYGEN,
                (probe_window(0, 0, 6), probe_window(1, 6, 12)),
            ) == (0,)

    run(exercise())
    assert len(requests) == 1
    assert requests[0].url.params["activityStartDateLower"] == "2024-01-01"
    assert requests[0].url.params["activityStartDateUpper"] == "2024-01-03"


def test_batch_samples_probe_does_not_assign_date_only_value_to_utc_day() -> None:
    header, row, _ = (
        (FIXTURES / "usgs_samples_narrow.csv").read_text(encoding="utf-8").splitlines()
    )
    date_only_row = row.replace("2024-01-01,08:30:00,EST", "2024-01-02,,")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=f"{header}\n{date_only_row}\n".encode(), request=request
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, samples_base_url=SAMPLES_BASE)
            assert (
                await adapter.get_sample_observation_windows(
                    sample_period(), OXYGEN, (probe_window(0, 0, 24),)
                )
                == ()
            )

    run(exercise())


def test_batch_samples_probe_reports_record_limit_before_declaring_empty() -> None:
    header, row, _ = (
        (FIXTURES / "usgs_samples_narrow.csv").read_text(encoding="utf-8").splitlines()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=f"{header}\n{row}\n{row}\n".encode(), request=request
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http, samples_base_url=SAMPLES_BASE, page_size=1, max_records=1
            )
            with pytest.raises(PortResponseLimitError):
                await adapter.get_sample_observation_windows(
                    sample_period(), OXYGEN, (probe_window(0, 0, 6),)
                )

    run(exercise())


def test_samples_probe_rejects_wrong_station_instead_of_reporting_empty() -> None:
    header, row, _ = (
        (FIXTURES / "usgs_samples_narrow.csv").read_text(encoding="utf-8").splitlines()
    )
    wrong_row = row.replace("USGS-01646500", "USGS-OTHER")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=f"{header}\n{wrong_row}\n".encode(), request=request
        )

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(http, samples_base_url=SAMPLES_BASE)
            with pytest.raises(InvalidPortResponseError):
                await adapter.get_sample_observation_windows(
                    sample_period(),
                    OXYGEN,
                    (
                        probe_range(
                            0,
                            datetime(2024, 1, 1, tzinfo=UTC),
                            datetime(2024, 1, 2, tzinfo=UTC),
                        ),
                    ),
                )

    run(exercise())


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


@pytest.mark.parametrize("has_data_row", [False, True])
def test_samples_alternate_empty_header_requires_no_data_rows(
    has_data_row: bool,
) -> None:
    content = (
        "monitoringLocationIdentifier,characteristicGroup,characteristic,"
        "characteristicUserSupplied,resultCount,activities,firstActivity,"
        "mostRecentActivity\n"
        + (
            "USGS-04021205,Physical,pH,pH,1,1,2026-10-01,2026-10-01\n"
            if has_data_row
            else ""
        )
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, request=request)

    async def exercise() -> None:
        async with provider_client(handler) as http:
            adapter = UsgsWaterDataAdapter(
                http,
                samples_base_url=SAMPLES_BASE,
            )
            if has_data_row:
                with pytest.raises(InvalidPortResponseError):
                    await adapter.get_sample_periods("USGS-04021205")
            else:
                assert await adapter.get_sample_periods("USGS-04021205") == ()

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
