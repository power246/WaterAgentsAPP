import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.application.query_understanding import AnalysisTimeSegment
from app.application.station_availability import (
    StationAvailabilityService,
    build_station_availability_request,
)
from app.domain import (
    AvailabilityCoverageSource,
    GeoPoint,
    InferenceMode,
    MonitoringLocationMetadata,
    MonitoringStation,
    ParameterKey,
    ParameterProvider,
    SamplePeriodOfRecord,
    TimeRange,
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

UTC = timezone.utc
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
    ) -> None:
        self.parameters = parameters
        self.locations = locations
        self.series = series
        self.samples = samples or {}
        self.errors = errors or {}
        self.delays = delays or {}
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
) -> SamplePeriodOfRecord:
    return SamplePeriodOfRecord(
        station_id=station_id,
        characteristic=characteristic,
        first_activity=first,
        most_recent_activity=last,
        result_count=5,
        activity_count=3,
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
                start=datetime(2026, 1, 5, tzinfo=UTC),
                end=datetime(2026, 1, 7, tzinfo=UTC),
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

    assert [(unit.parameter, unit.segment_index) for unit in result.coverage_units] == [
        (DISCHARGE, 0),
        (GAGE_HEIGHT, 0),
        (DISCHARGE, 1),
        (GAGE_HEIGHT, 1),
    ]
    assert result.historical_ranges[0].start == datetime(2026, 1, 1, tzinfo=UTC)
    baseline = result.prediction_baseline_ranges[0]
    assert baseline.end == datetime(2026, 1, 5, tzinfo=UTC)
    assert baseline.end - baseline.start == timedelta(hours=48)


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
                end=boundary - timedelta(microseconds=1),
                identity="boundary-outside",
            ),
        ),
    )

    result = run(
        service(FakeTopology((associated,)), metadata).check_availability(
            request(GAGE_HEIGHT, mixed=True)
        )
    )

    assert [(unit.parameter, unit.segment_index) for unit in result[0].covered_units] == [
        (GAGE_HEIGHT, 0),
        (GAGE_HEIGHT, 1),
    ]
    assert {
        reference.coverage_source for reference in result[0].series_refs
    } == {
        AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_ENVELOPE,
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


def test_expired_deadline_stops_before_starting_new_provider_calls() -> None:
    associated = station()
    topology = FakeTopology((associated,), expire_deadline=True)
    metadata = FakeMetadata()

    async def exercise() -> None:
        with pytest.raises(PortTimeoutError):
            await service(topology, metadata).check_availability(
                request(GAGE_HEIGHT),
                deadline=asyncio.get_running_loop().time() + 0.001,
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
) -> None:
    with pytest.raises(InvalidPortResponseError):
        run(
            service(FakeTopology((station(),)), metadata).check_availability(
                request(DISCHARGE, supporting=())
            )
        )


def test_hydrologic_association_and_sample_station_identity_are_required() -> None:
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
    with pytest.raises(InvalidPortResponseError):
        run(
            service(FakeTopology((associated,)), metadata).check_availability(
                request(SECCHI)
            )
        )
