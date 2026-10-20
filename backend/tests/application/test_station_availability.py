import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from app.application.query_understanding import AnalysisTimeSegment
from app.application.station_availability import (
    StationAvailabilityService,
    build_station_availability_request,
)
from app.domain import (
    AvailabilityCoverageSource,
    AvailabilityTimeRange,
    CoverageUnit,
    GeoPoint,
    InferenceMode,
    MonitoringLocationMetadata,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    SamplePeriodOfRecord,
    StationAvailabilityRequest,
    TimeRange,
    TimeResolution,
    TimeSeriesKind,
    TimeSeriesMetadata,
    UsgsParameterMetadata,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import (
    InvalidPortResponseError,
    PortRateLimitError,
    PortTimeoutError,
)

CHECKED_AT = datetime(2026, 9, 27, 8, tzinfo=UTC)
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
GAGE_HEIGHT = ParameterKey(code_system="usgs", code="00065")
SECCHI = ParameterKey(code_system="usgs", code="00078")
AMMONIA_FILTERED = ParameterKey(code_system="usgs", code="00608")
OTHER_PARAMETER = ParameterKey(code_system="usgs", code="00010")
PH = ParameterKey(code_system="usgs", code="00400")


class FixedClock:
    def now(self) -> datetime:
        return CHECKED_AT


class FakeTopology:
    def __init__(
        self,
        stations: tuple[MonitoringStation, ...] = (),
        *,
        error: Exception | None = None,
        expire_deadline: bool = False,
    ) -> None:
        self.stations = stations
        self.error = error
        self.expire_deadline = expire_deadline
        self.calls = 0

    async def get_monitoring_sites(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> tuple[MonitoringStation, ...]:
        self.calls += 1
        if self.expire_deadline:
            assert deadline is not None
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.stations


class FakeMetadata:
    def __init__(
        self,
        *,
        parameters: tuple[UsgsParameterMetadata, ...] = (),
        locations: tuple[MonitoringLocationMetadata, ...] = (),
        series: tuple[TimeSeriesMetadata, ...] = (),
        samples: dict[str, tuple[SamplePeriodOfRecord, ...]] | None = None,
        errors: dict[str, Exception] | None = None,
        delays: dict[str, float] | None = None,
        series_probe: dict[tuple[str, int], bool] | None = None,
        sample_probe: dict[tuple[str, int], bool] | None = None,
    ) -> None:
        self.parameters = parameters
        self.locations = locations
        self.series = series
        self.samples = samples or {}
        self.errors = errors or {}
        self.delays = delays or {}
        self.series_probe = series_probe or {}
        self.sample_probe = sample_probe or {}
        self.calls: list[tuple[str, object]] = []

    def _raise(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error

    async def _delay(self, operation: str) -> None:
        delay = self.delays.get(operation, 0)
        if delay:
            await asyncio.sleep(delay)

    async def get_parameter_codes(self, parameters, *, deadline=None):
        self.calls.append(("parameters", parameters))
        await self._delay("parameters")
        self._raise("parameters")
        return self.parameters

    async def get_monitoring_locations(self, station_ids, *, deadline=None):
        self.calls.append(("locations", station_ids))
        await self._delay("locations")
        self._raise("locations")
        return self.locations

    async def get_time_series_metadata(
        self,
        station_ids,
        parameters,
        *,
        deadline=None,
    ):
        self.calls.append(("series", (station_ids, parameters)))
        await self._delay("series")
        self._raise("series")
        return self.series

    async def get_sample_periods(self, station_id, *, deadline=None):
        self.calls.append(("samples", station_id))
        await self._delay("samples")
        self._raise("samples")
        return self.samples.get(station_id, ())

    async def get_time_series_observation_windows(
        self, series, requested, *, deadline=None
    ):
        indexes = tuple(item.window_index for item in requested)
        self.calls.append(("series_probe", (series.series_id, indexes)))
        await self._delay("series_probe")
        self._raise("series_probe")
        return tuple(
            index
            for index in indexes
            if self.series_probe.get((series.series_id, index), True)
        )

    async def get_sample_observation_windows(
        self, period, parameter, requested, *, deadline=None
    ):
        indexes = tuple(item.window_index for item in requested)
        self.calls.append(("sample_probe", (period.provider_identity, indexes)))
        await self._delay("sample_probe")
        self._raise("sample_probe")
        return tuple(
            index
            for index in indexes
            if self.sample_probe.get((period.provider_identity, index), True)
        )


def run(coroutine):
    return asyncio.run(coroutine)


def station(
    station_id: str = "USGS-A",
    *,
    comid: str = "12345678",
) -> MonitoringStation:
    return MonitoringStation(
        station_id=station_id,
        source="nwissite",
        name=f"Station {station_id}",
        location=GeoPoint(latitude=38.9, longitude=-77.0),
        associated_comid=comid,
    )


def parameter_metadata(key: ParameterKey) -> UsgsParameterMetadata:
    capability = load_parameter_capability_registry().get(key)
    assert capability is not None
    return UsgsParameterMetadata(
        key=key,
        name=capability.official_name,
        unit=capability.official_unit,
        provider_identity=f"directory:{key.code}",
    )


def location(station_id: str) -> MonitoringLocationMetadata:
    return MonitoringLocationMetadata(
        station_id=station_id,
        agency_code="USGS",
        name=f"Station {station_id}",
        location=GeoPoint(latitude=38.9, longitude=-77.0),
        provider_identity=f"location:{station_id}",
    )


def time_series(
    station_id: str,
    parameter: ParameterKey,
    *,
    begin: datetime,
    end: datetime,
    identity: str,
    kind: TimeSeriesKind = TimeSeriesKind.CONTINUOUS,
    unit: str | None = None,
) -> TimeSeriesMetadata:
    capability = load_parameter_capability_registry().get(parameter)
    assert capability is not None
    return TimeSeriesMetadata(
        series_id=identity,
        station_id=station_id,
        parameter=parameter,
        unit=unit or capability.official_unit,
        begin=begin,
        end=end,
        kind=kind,
        provider_identity=f"wdfn:v1:{identity}",
    )


def sample_period(
    station_id: str,
    characteristic: str,
    *,
    first: date = date(2025, 1, 1),
    last: date = date(2026, 9, 27),
    identity: str | None = None,
    result_count: int = 5,
    activity_count: int = 3,
) -> SamplePeriodOfRecord:
    return SamplePeriodOfRecord(
        station_id=station_id,
        characteristic=characteristic,
        first_activity=first,
        most_recent_activity=last,
        result_count=result_count,
        activity_count=activity_count,
        provider_identity=identity or f"samples:v1:{station_id}:{characteristic}",
    )


def segments(*, mixed: bool) -> tuple[AnalysisTimeSegment, ...]:
    historical = AnalysisTimeSegment(
        segment_index=0,
        mode=InferenceMode.GAP_FILLING,
        time_range=TimeRange(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 3, tzinfo=UTC),
        ),
    )
    if not mixed:
        return (historical,)
    return (
        historical,
        AnalysisTimeSegment(
            segment_index=1,
            mode=InferenceMode.PREDICTION,
            time_range=TimeRange(
                start=datetime(2026, 1, 3, tzinfo=UTC),
                end=datetime(2026, 1, 5, tzinfo=UTC),
            ),
        ),
    )


def request(
    *targets: ParameterKey,
    mixed: bool = False,
    supporting: tuple[ParameterKey, ...] = (DISCHARGE,),
):
    return build_station_availability_request(
        comid="12345678",
        target_parameters=targets,
        supporting_parameters=supporting,
        time_segments=segments(mixed=mixed),
        time_resolution=TimeResolution.TWELVE_HOURS,
        analysis_timezone="UTC",
    )


def service(topology: FakeTopology, metadata: FakeMetadata):
    return StationAvailabilityService(
        topology=topology,
        metadata=metadata,
        capability_registry=load_parameter_capability_registry(),
        clock=FixedClock(),
    )


def test_request_builder_deduplicates_discharge_and_builds_mixed_ranges() -> None:
    result = request(
        DISCHARGE,
        GAGE_HEIGHT,
        mixed=True,
        supporting=(DISCHARGE,),
    )

    assert [(unit.parameter, unit.segment_index, unit.window_index) for unit in result.coverage_units] == [
        (parameter, 0, window_index)
        for window_index in range(4)
        for parameter in (DISCHARGE, GAGE_HEIGHT)
    ] + [
        (DISCHARGE, 1, None),
        (GAGE_HEIGHT, 1, None),
    ]
    assert result.historical_ranges[0].start == datetime(2026, 1, 1, tzinfo=UTC)
    baseline = result.prediction_baseline_ranges[0]
    assert baseline.end == datetime(2026, 1, 3, tzinfo=UTC)
    assert baseline.end - baseline.start == timedelta(hours=48)


def test_request_rejects_reused_historical_window_id_across_segments() -> None:
    with pytest.raises(ValueError, match="historical window indexes"):
        StationAvailabilityRequest(
            comid="12345678",
            coverage_units=(
                CoverageUnit(parameter=DISCHARGE, segment_index=0, window_index=0),
                CoverageUnit(parameter=DISCHARGE, segment_index=1, window_index=0),
            ),
            historical_ranges=(
                AvailabilityTimeRange(
                    segment_index=0,
                    window_index=0,
                    start=datetime(2026, 1, 1, tzinfo=UTC),
                    end=datetime(2026, 1, 2, tzinfo=UTC),
                ),
                AvailabilityTimeRange(
                    segment_index=1,
                    window_index=0,
                    start=datetime(2026, 1, 2, tzinfo=UTC),
                    end=datetime(2026, 1, 3, tzinfo=UTC),
                ),
            ),
            prediction_baseline_ranges=(),
        )


def test_historical_ts_and_samples_routes_preserve_partial_coverage() -> None:
    associated = station()
    metadata = FakeMetadata(
        parameters=(parameter_metadata(DISCHARGE), parameter_metadata(GAGE_HEIGHT)),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                GAGE_HEIGHT,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                identity="gage-height",
            ),
            time_series(
                associated.station_id,
                DISCHARGE,
                begin=datetime(2026, 1, 2, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                identity="late-discharge",
            ),
        ),
        series_probe={("late-discharge", index): False for index in range(4)},
        samples={
            associated.station_id: (
                sample_period(
                    associated.station_id,
                    "Transparency, Secchi",
                ),
                sample_period(associated.station_id, "Unmapped characteristic"),
            )
        },
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(GAGE_HEIGHT, SECCHI)
        )
    )

    assert len(result) == 1
    qualification = result[0]
    assert qualification.station == associated
    assert qualification.checked_at == CHECKED_AT
    assert qualification.providers == (
        ParameterProvider.USGS_SAMPLES,
        ParameterProvider.WDFN_TIME_SERIES,
    )
    assert {unit.parameter for unit in qualification.covered_units} == {
        GAGE_HEIGHT,
        SECCHI,
    }
    assert DISCHARGE not in {
        unit.parameter for unit in qualification.covered_units
    }
    parameter_call = next(value for name, value in metadata.calls if name == "parameters")
    assert parameter_call == (DISCHARGE, GAGE_HEIGHT)
    assert [name for name, _ in metadata.calls].count("samples") == 1
    assert [name for name, _ in metadata.calls].count("series_probe") == 2
    assert [name for name, _ in metadata.calls].count("sample_probe") == 1


def test_ph_official_unit_qualifies_parameter_and_time_series_metadata() -> None:
    associated = station()
    metadata = FakeMetadata(
        parameters=(parameter_metadata(DISCHARGE), parameter_metadata(PH)),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                identity="ph-series",
                unit="pH Units",
            ),
            time_series(
                associated.station_id,
                DISCHARGE,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                identity="discharge-series",
                unit="ft^3/s",
            ),
        ),
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(PH)
        )
    )

    assert len(result) == 1
    assert {unit.parameter for unit in result[0].covered_units} == {PH, DISCHARGE}
    assert {reference.series_identity for reference in result[0].series_refs} == {
        "ph-series",
        "discharge-series",
    }


def test_historical_qualification_uses_observations_per_window_not_metadata_span() -> (
    None
):
    associated = station()
    history = (
        AnalysisTimeSegment(
            segment_index=0,
            mode=InferenceMode.GAP_FILLING,
            time_range=TimeRange(
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 1, 3, tzinfo=UTC),
            ),
        ),
    )
    historical_request = build_station_availability_request(
        comid="12345678",
        target_parameters=(PH,),
        supporting_parameters=(DISCHARGE,),
        time_segments=history,
        time_resolution=TimeResolution.TWENTY_FOUR_HOURS,
        analysis_timezone="UTC",
    )
    metadata = FakeMetadata(
        parameters=(parameter_metadata(PH),),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2026, 1, 1, tzinfo=UTC),
                identity="stale-ph",
                unit="pH Units",
            ),
        ),
        series_probe={("stale-ph", 0): True, ("stale-ph", 1): False},
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            historical_request
        )
    )

    assert [
        (unit.parameter, unit.segment_index, unit.window_index)
        for unit in result[0].covered_units
    ] == [(PH, 0, 0)]
    assert result[0].series_refs[0].coverage_source is (
        AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION
    )
    assert [value for name, value in metadata.calls if name == "series_probe"] == [
        ("stale-ph", (0, 1)),
    ]


def test_full_historical_metadata_envelope_without_observation_is_not_qualified() -> (
    None
):
    associated = station()
    metadata = FakeMetadata(
        parameters=(parameter_metadata(PH),),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2027, 1, 1, tzinfo=UTC),
                identity="empty-ph",
                unit="pH Units",
            ),
        ),
        series_probe={("empty-ph", index): False for index in range(4)},
    )

    assert (
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(PH)
            )
        )
        == ()
    )


def test_sample_period_dates_do_not_replace_actual_historical_observation() -> None:
    associated = station()
    metadata = FakeMetadata(
        samples={
            associated.station_id: (
                sample_period(
                    associated.station_id,
                    "Transparency, Secchi",
                    last=date(2025, 1, 1),
                    identity="stale-secchi",
                ),
            )
        },
        sample_probe={
            ("stale-secchi", index): index == 0 for index in range(4)
        },
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(SECCHI)
        )
    )
    sample_ref = next(
        reference
        for reference in result[0].series_refs
        if reference.provider is ParameterProvider.USGS_SAMPLES
    )
    assert sample_ref.coverage_source is (
        AvailabilityCoverageSource.HISTORICAL_SAMPLE_OBSERVATION
    )
    assert [unit.window_index for unit in sample_ref.covered_units] == [0]


def test_sample_summary_zero_count_does_not_suppress_actual_observation() -> None:
    associated = station()
    metadata = FakeMetadata(
        samples={
            associated.station_id: (
                sample_period(
                    associated.station_id,
                    "Transparency, Secchi",
                    identity="stale-count-secchi",
                    result_count=0,
                    activity_count=0,
                ),
            )
        },
        sample_probe={("stale-count-secchi", index): index == 0 for index in range(4)},
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(SECCHI)
        )
    )

    assert [unit.window_index for unit in result[0].covered_units] == [0]
    assert [name for name, _ in metadata.calls].count("sample_probe") == 1


def test_sample_summary_zero_count_does_not_claim_prediction_baseline() -> None:
    associated = station()
    metadata = FakeMetadata(
        samples={
            associated.station_id: (
                sample_period(
                    associated.station_id,
                    "Transparency, Secchi",
                    identity="empty-secchi",
                    result_count=0,
                    activity_count=0,
                ),
            )
        },
        sample_probe={("empty-secchi", index): False for index in range(4)},
    )

    assert (
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(SECCHI, mixed=True)
            )
        )
        == ()
    )


def test_historical_probe_failure_propagates_instead_of_appearing_empty() -> None:
    associated = station()
    failure = PortTimeoutError(
        "probe failed",
        provider="fixture",
        operation="get_time_series_observation_windows",
    )
    metadata = FakeMetadata(
        parameters=(parameter_metadata(PH),),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2027, 1, 1, tzinfo=UTC),
                identity="ph",
                unit="pH Units",
            ),
        ),
        errors={"series_probe": failure},
    )
    with pytest.raises(PortTimeoutError) as captured:
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(PH)
            )
        )
    assert captured.value is failure


def test_historical_probe_rejects_unrequested_window_id() -> None:
    class InvalidWindowMetadata(FakeMetadata):
        async def get_time_series_observation_windows(
            self, series, requested, *, deadline=None
        ):
            return (99,)

    associated = station()
    metadata = InvalidWindowMetadata(
        parameters=(parameter_metadata(PH),),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2027, 1, 1, tzinfo=UTC),
                identity="ph",
                unit="pH Units",
            ),
        ),
    )
    with pytest.raises(InvalidPortResponseError, match="invalid window indexes"):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(PH)
            )
        )


def test_mixed_segments_apply_prediction_boundary_without_covering_older_data() -> None:
    associated = station()
    boundary = datetime(2026, 1, 3, tzinfo=UTC)
    metadata = FakeMetadata(
        parameters=(parameter_metadata(GAGE_HEIGHT), parameter_metadata(DISCHARGE)),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                GAGE_HEIGHT,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=boundary,
                identity="boundary-inclusive",
                kind=TimeSeriesKind.DAILY,
            ),
            time_series(
                associated.station_id,
                DISCHARGE,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
                identity="boundary-outside",
            ),
        ),
        series_probe={("boundary-outside", index): False for index in range(4)},
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(GAGE_HEIGHT, mixed=True)
        )
    )

    assert [
        (unit.parameter, unit.segment_index, unit.window_index)
        for unit in result[0].covered_units
    ] == [(GAGE_HEIGHT, 0, index) for index in range(4)] + [
        (GAGE_HEIGHT, 1, None)
    ]
    assert {
        reference.coverage_source for reference in result[0].series_refs
    } == {
        AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION,
        AvailabilityCoverageSource.PREDICTION_TIME_SERIES_BASELINE,
    }


def test_samples_require_unambiguous_filtered_identity() -> None:
    associated = station()
    metadata = FakeMetadata(
        samples={
            associated.station_id: (
                sample_period(
                    associated.station_id,
                    "Ammonia",
                    identity="ambiguous-ammonia",
                ),
                sample_period(
                    associated.station_id,
                    "Ammonia, filtered",
                    identity="filtered-ammonia",
                ),
            )
        }
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(AMMONIA_FILTERED)
        )
    )

    assert len(result) == 1
    sample_refs = [
        reference
        for reference in result[0].series_refs
        if reference.provider is ParameterProvider.USGS_SAMPLES
    ]
    assert [reference.series_identity for reference in sample_refs] == [
        "filtered-ammonia"
    ]
    assert sample_refs[0].parameter == AMMONIA_FILTERED


def test_station_and_provider_input_order_do_not_change_serialization() -> None:
    stations = (station("USGS-A"), station("USGS-B"))
    directory = (parameter_metadata(DISCHARGE),)
    locations = tuple(location(item.station_id) for item in stations)
    series = tuple(
        time_series(
            item.station_id,
            DISCHARGE,
            begin=datetime(2025, 12, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
            identity=f"discharge-{item.station_id}",
        )
        for item in stations
    )

    first = run(
        service(
            FakeTopology(tuple(reversed(stations))),
            FakeMetadata(
                parameters=directory,
                locations=tuple(reversed(locations)),
                series=tuple(reversed(series)),
                delays={"parameters": 0.003},
            ),
        ).check_availability(request(DISCHARGE, supporting=()))
    )
    second = run(
        service(
            FakeTopology(stations),
            FakeMetadata(
                parameters=directory,
                locations=locations,
                series=series,
                delays={"samples": 0.003},
            ),
        ).check_availability(request(DISCHARGE, supporting=()))
    )

    assert [item.station.station_id for item in first] == ["USGS-A", "USGS-B"]
    assert tuple(item.model_dump_json() for item in first) == tuple(
        item.model_dump_json() for item in second
    )


def test_successful_empty_and_provider_failure_remain_distinct() -> None:
    empty_metadata = FakeMetadata()
    assert run(
        service(FakeTopology(), empty_metadata).check_availability(
            request(GAGE_HEIGHT)
        )
    ) == ()
    assert empty_metadata.calls == []

    associated = station()
    metadata_empty = FakeMetadata()
    assert run(
        service(FakeTopology((associated,)), metadata_empty).check_availability(
            request(GAGE_HEIGHT)
        )
    ) == ()
    assert [name for name, _ in metadata_empty.calls] == [
        "parameters",
        "samples",
    ]

    failure = PortRateLimitError("limited", provider="fixture", operation="sites")
    with pytest.raises(PortRateLimitError) as captured:
        run(
            service(
                FakeTopology(error=failure),
                FakeMetadata(),
            ).check_availability(request(GAGE_HEIGHT))
        )
    assert captured.value is failure

    timeout = PortTimeoutError(
        "timed out",
        provider="fixture",
        operation="parameters",
    )
    with pytest.raises(PortTimeoutError) as captured_timeout:
        run(
            service(
                FakeTopology((associated,)),
                FakeMetadata(errors={"parameters": timeout}),
            ).check_availability(request(GAGE_HEIGHT))
        )
    assert captured_timeout.value is timeout


def test_time_series_probe_failure_logs_series_identity_and_preserves_error(caplog) -> None:
    associated = station()
    failure = PortRateLimitError(
        "limited",
        provider="usgs_waterdata",
        operation="get_time_series_observation_windows",
        status_code=429,
    )
    metadata = FakeMetadata(
        parameters=(parameter_metadata(PH),),
        locations=(location(associated.station_id),),
        series=(
            time_series(
                associated.station_id,
                PH,
                begin=datetime(2025, 12, 1, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                identity="ph-series",
                unit="pH Units",
            ),
        ),
        errors={"series_probe": failure},
    )

    with (
        caplog.at_level(logging.WARNING, logger="wateragents.station_availability"),
        pytest.raises(PortRateLimitError) as captured,
    ):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(PH)
            )
        )

    assert captured.value is failure
    record = next(
        item for item in caplog.records
        if getattr(item, "event_name", None) == "station_time_series_probe_failed"
    )
    assert record.station_id == associated.station_id
    assert record.series_id == "ph-series"
    assert record.parameter_code == PH.code
    assert record.provider_operation == failure.operation
    assert record.provider_error == "PortRateLimitError"
    assert record.provider_status == 429


def test_sample_period_lookup_failure_logs_station_and_preserves_error(caplog) -> None:
    associated = station()
    failure = PortRateLimitError(
        "limited",
        provider="usgs_samples",
        operation="get_sample_periods",
        status_code=429,
    )
    metadata = FakeMetadata(errors={"samples": failure})

    with (
        caplog.at_level(logging.WARNING, logger="wateragents.station_availability"),
        pytest.raises(PortRateLimitError) as captured,
    ):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(SECCHI)
            )
        )

    assert captured.value is failure
    record = next(
        item for item in caplog.records
        if getattr(item, "event_name", None) == "station_sample_periods_failed"
    )
    assert record.station_id == associated.station_id
    assert record.provider_operation == failure.operation
    assert record.provider_error == "PortRateLimitError"
    assert record.provider_status == 429


def test_sample_probe_failure_logs_sample_identity_and_preserves_error(caplog) -> None:
    associated = station()
    period = sample_period(
        associated.station_id,
        "Transparency, Secchi",
        identity="secchi-period",
    )
    failure = PortRateLimitError(
        "limited",
        provider="usgs_samples",
        operation="get_sample_observation_windows",
        status_code=429,
    )
    metadata = FakeMetadata(
        samples={associated.station_id: (period,)},
        errors={"sample_probe": failure},
    )

    with (
        caplog.at_level(logging.WARNING, logger="wateragents.station_availability"),
        pytest.raises(PortRateLimitError) as captured,
    ):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(SECCHI)
            )
        )

    assert captured.value is failure
    record = next(
        item for item in caplog.records
        if getattr(item, "event_name", None) == "station_sample_probe_failed"
    )
    assert record.station_id == associated.station_id
    assert record.sample_identity == period.provider_identity
    assert record.parameter_code == SECCHI.code
    assert record.provider_operation == failure.operation
    assert record.provider_error == "PortRateLimitError"
    assert record.provider_status == 429


def test_expired_deadline_stops_before_starting_new_provider_calls() -> None:
    associated = station()
    topology = FakeTopology((associated,), expire_deadline=True)
    metadata = FakeMetadata()

    async def exercise() -> None:
        availability_service = service(topology, metadata)
        availability_request = request(GAGE_HEIGHT)
        with pytest.raises(PortTimeoutError):
            await availability_service.check_availability(
                availability_request,
                deadline=asyncio.get_running_loop().time() + 0.5,
            )

    run(exercise())
    assert topology.calls == 1
    assert metadata.calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        FakeMetadata(
            parameters=(
                UsgsParameterMetadata(
                    key=DISCHARGE,
                    name="Wrong directory identity",
                    unit="ft3/s",
                    provider_identity="wrong-directory",
                ),
            )
        ),
        FakeMetadata(
            parameters=(parameter_metadata(DISCHARGE),),
            locations=(location("USGS-A"),),
            series=(
                time_series(
                    "USGS-A",
                    OTHER_PARAMETER,
                    begin=datetime(2025, 12, 1, tzinfo=UTC),
                    end=datetime(2026, 2, 1, tzinfo=UTC),
                    identity="wrong-series-parameter",
                ),
            ),
        ),
    ],
)
def test_directory_and_series_identity_drift_are_invalid(
    metadata: FakeMetadata,
    caplog,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="wateragents.station_availability"),
        pytest.raises(InvalidPortResponseError),
    ):
        run(
            service(FakeTopology((station(),)), metadata).check_availability(
                request(DISCHARGE, supporting=())
            )
        )
    record = next(
        item for item in caplog.records
        if getattr(item, "event_name", None) == "station_metadata_invalid"
    )
    assert record.provider_error == "InvalidPortResponseError"
    if metadata.series:
        assert record.provider_operation == "get_time_series_metadata"
        assert record.station_id == "USGS-A"
        assert record.series_id == "wrong-series-parameter"
        assert record.parameter_code == OTHER_PARAMETER.code
    else:
        assert record.provider_operation == "get_parameter_codes"
        assert record.parameter_code == DISCHARGE.code


def test_hydrologic_association_and_sample_station_identity_are_required(caplog) -> None:
    with pytest.raises(InvalidPortResponseError):
        run(
            service(
                FakeTopology((station(comid="99999999"),)),
                FakeMetadata(),
            ).check_availability(request(SECCHI))
        )

    associated = station()
    metadata = FakeMetadata(
        samples={
            associated.station_id: (
                sample_period("USGS-OTHER", "Transparency, Secchi"),
            )
        }
    )
    with (
        caplog.at_level(logging.WARNING, logger="wateragents.station_availability"),
        pytest.raises(InvalidPortResponseError),
    ):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(SECCHI)
            )
        )
    record = next(
        item for item in caplog.records
        if getattr(item, "provider_operation", None) == "get_sample_periods"
    )
    assert record.station_id == "USGS-OTHER"
    assert record.sample_identity == "samples:v1:USGS-OTHER:Transparency, Secchi"
