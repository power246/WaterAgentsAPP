from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.domain import (
    AvailabilityCoverageSource,
    AvailabilityTimeRange,
    CoverageUnit,
    MonitoringLocationMetadata,
    MonitoringStation,
    ParameterCapability,
    ParameterCapabilityRegistry,
    ParameterKey,
    ParameterProvider,
    SamplePeriodOfRecord,
    SeriesRef,
    StationAvailability,
    StationAvailabilityRequest,
    TimeSeriesMetadata,
    UsgsParameterMetadata,
    normalize_parameter_concept,
)
from app.ports import (
    ClockPort,
    InvalidPortResponseError,
    PortError,
    PortTimeoutError,
    StationMetadataPort,
    TopologyDataPort,
)

PROVIDER = "station_inventory"
logger = logging.getLogger("wateragents.station_availability")


@dataclass(frozen=True)
class _AvailabilityPlan:
    units_by_parameter: dict[ParameterKey, tuple[CoverageUnit, ...]]
    historical_ranges: dict[tuple[int, int], AvailabilityTimeRange]
    prediction_baseline_ranges: dict[int, AvailabilityTimeRange]

    @classmethod
    def compile(cls, request: StationAvailabilityRequest) -> _AvailabilityPlan:
        units: dict[ParameterKey, list[CoverageUnit]] = {}
        for unit in request.coverage_units:
            units.setdefault(unit.parameter, []).append(unit)
        return cls(
            units_by_parameter={
                parameter: tuple(parameter_units)
                for parameter, parameter_units in units.items()
            },
            historical_ranges={
                (item.segment_index, item.window_index): item
                for item in request.historical_ranges
                if item.window_index is not None
            },
            prediction_baseline_ranges={
                item.segment_index: item
                for item in request.prediction_baseline_ranges
            },
        )


class StationAvailabilityService:
    def __init__(
        self,
        *,
        topology: TopologyDataPort,
        metadata: StationMetadataPort,
        capability_registry: ParameterCapabilityRegistry,
        clock: ClockPort,
    ) -> None:
        self._topology = topology
        self._metadata = metadata
        self._registry = capability_registry
        self._clock = clock
        self._sample_key_by_label = self._build_sample_label_index()
        self._probe_semaphore = asyncio.Semaphore(4)

    async def check_availability(
        self,
        request: StationAvailabilityRequest,
        *,
        deadline: float | None = None,
    ) -> tuple[StationAvailability, ...]:
        capabilities = self._capabilities(request)
        plan = _AvailabilityPlan.compile(request)
        self._ensure_deadline(deadline)
        associated = await self._topology.get_monitoring_sites(
            request.comid,
            deadline=deadline,
        )
        stations = self._associated_stations(
            associated,
            comid=request.comid,
        )
        if not stations:
            return ()

        time_series_parameters = self._routed_parameters(
            capabilities,
            ParameterProvider.WDFN_TIME_SERIES,
        )
        sample_parameters = self._routed_parameters(
            capabilities,
            ParameterProvider.USGS_SAMPLES,
        )
        references = await self._provider_references(
            plan,
            stations,
            time_series_parameters,
            sample_parameters,
            capabilities,
            deadline,
        )

        references_by_station: dict[str, list[SeriesRef]] = {
            station_id: [] for station_id in stations
        }
        for reference in references:
            references_by_station[reference.station_id].append(reference)

        checked_at = self._clock.now()
        results: list[StationAvailability] = []
        for station_id in sorted(stations):
            station_references = tuple(
                sorted(
                    references_by_station[station_id],
                    key=self._series_ref_sort_key,
                )
            )
            if not station_references:
                continue
            covered_units = tuple(
                sorted(
                    {
                        unit
                        for reference in station_references
                        for unit in reference.covered_units
                    },
                    key=self._coverage_sort_key,
                )
            )
            providers = tuple(
                sorted(
                    {reference.provider for reference in station_references},
                    key=lambda provider: provider.value,
                )
            )
            results.append(
                StationAvailability(
                    station=stations[station_id],
                    comid=request.comid,
                    providers=providers,
                    covered_units=covered_units,
                    series_refs=station_references,
                    checked_at=checked_at,
                )
            )
        return tuple(results)

    async def _provider_references(
        self,
        plan: _AvailabilityPlan,
        stations: dict[str, MonitoringStation],
        time_series_parameters: tuple[ParameterKey, ...],
        sample_parameters: tuple[ParameterKey, ...],
        capabilities: dict[ParameterKey, ParameterCapability],
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        self._ensure_deadline(deadline)
        tasks: list[asyncio.Task[tuple[SeriesRef, ...]]] = []
        if time_series_parameters:
            tasks.append(
                asyncio.create_task(
                    self._time_series_references(
                        plan,
                        stations,
                        time_series_parameters,
                        capabilities,
                        deadline,
                    )
                )
            )
        if sample_parameters:
            tasks.append(
                asyncio.create_task(
                    self._sample_references(
                        plan,
                        stations,
                        sample_parameters,
                        deadline,
                    )
                )
            )
        try:
            groups = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return tuple(reference for group in groups for reference in group)

    def _capabilities(
        self,
        request: StationAvailabilityRequest,
    ) -> dict[ParameterKey, ParameterCapability]:
        capabilities: dict[ParameterKey, ParameterCapability] = {}
        for unit in request.coverage_units:
            capability = self._registry.get(unit.parameter)
            if capability is None:
                raise ValueError("availability parameter is not executable")
            capabilities[unit.parameter] = capability
        return capabilities

    @staticmethod
    def _routed_parameters(
        capabilities: dict[ParameterKey, ParameterCapability],
        provider: ParameterProvider,
    ) -> tuple[ParameterKey, ...]:
        return tuple(
            sorted(
                (
                    key
                    for key, capability in capabilities.items()
                    if provider in capability.availability_providers
                ),
                key=lambda key: (key.code_system, key.code),
            )
        )

    def _associated_stations(
        self,
        stations: tuple[MonitoringStation, ...],
        *,
        comid: str,
    ) -> dict[str, MonitoringStation]:
        by_id: dict[str, MonitoringStation] = {}
        for station in stations:
            if station.associated_comid != comid:
                raise self._invalid_metadata(
                    "get_monitoring_sites", station_id=station.station_id, comid=comid
                )
            previous = by_id.get(station.station_id)
            if previous is not None and previous != station:
                raise self._invalid_metadata(
                    "get_monitoring_sites", station_id=station.station_id, comid=comid
                )
            by_id[station.station_id] = station
        return by_id

    async def _time_series_references(
        self,
        plan: _AvailabilityPlan,
        stations: dict[str, MonitoringStation],
        parameters: tuple[ParameterKey, ...],
        capabilities: dict[ParameterKey, ParameterCapability],
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        self._ensure_deadline(deadline)
        raw_parameters = await self._metadata.get_parameter_codes(
            parameters,
            deadline=deadline,
        )
        parameter_directory = self._parameter_directory(
            raw_parameters,
            requested=set(parameters),
            capabilities=capabilities,
        )
        if not parameter_directory:
            return ()

        station_ids = tuple(sorted(stations))
        self._ensure_deadline(deadline)
        raw_locations = await self._metadata.get_monitoring_locations(
            station_ids,
            deadline=deadline,
        )
        location_directory = self._location_directory(
            raw_locations,
            requested=set(station_ids),
        )
        if not location_directory:
            return ()

        routed_stations = tuple(sorted(location_directory))
        routed_parameters = tuple(
            sorted(
                parameter_directory,
                key=lambda key: (key.code_system, key.code),
            )
        )
        self._ensure_deadline(deadline)
        series = await self._metadata.get_time_series_metadata(
            routed_stations,
            routed_parameters,
            deadline=deadline,
        )
        return await self._qualify_time_series(
            plan,
            series,
            stations=set(routed_stations),
            parameters=set(routed_parameters),
            capabilities=capabilities,
            deadline=deadline,
        )

    def _parameter_directory(
        self,
        records: tuple[UsgsParameterMetadata, ...],
        *,
        requested: set[ParameterKey],
        capabilities: dict[ParameterKey, ParameterCapability],
    ) -> dict[ParameterKey, UsgsParameterMetadata]:
        by_key: dict[ParameterKey, UsgsParameterMetadata] = {}
        for record in records:
            capability = capabilities.get(record.key)
            if record.key not in requested or capability is None:
                raise self._invalid_metadata(
                    "get_parameter_codes", parameter_code=record.key.code
                )
            if (
                record.name != capability.official_name
                or record.unit != capability.official_unit
            ):
                raise self._invalid_metadata(
                    "get_parameter_codes", parameter_code=record.key.code
                )
            if record.key in by_key:
                raise self._invalid_metadata(
                    "get_parameter_codes", parameter_code=record.key.code
                )
            by_key[record.key] = record
        return by_key

    def _location_directory(
        self,
        records: tuple[MonitoringLocationMetadata, ...],
        *,
        requested: set[str],
    ) -> dict[str, MonitoringLocationMetadata]:
        by_id: dict[str, MonitoringLocationMetadata] = {}
        for record in records:
            if record.station_id not in requested or record.station_id in by_id:
                raise self._invalid_metadata(
                    "get_monitoring_locations", station_id=record.station_id
                )
            by_id[record.station_id] = record
        return by_id

    async def _qualify_time_series(
        self,
        plan: _AvailabilityPlan,
        records: tuple[TimeSeriesMetadata, ...],
        *,
        stations: set[str],
        parameters: set[ParameterKey],
        capabilities: dict[ParameterKey, ParameterCapability],
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        seen_identities: set[str] = set()
        references: list[SeriesRef] = []
        for record in records:
            capability = capabilities.get(record.parameter)
            source_unit = self._registry.resolve_unit(record.unit)
            if (
                record.station_id not in stations
                or record.parameter not in parameters
                or capability is None
                or source_unit is None
                or source_unit.unit_id not in capability.accepted_source_units
                or record.series_id in seen_identities
            ):
                raise self._invalid_metadata(
                    "get_time_series_metadata",
                    station_id=record.station_id,
                    series_id=record.series_id,
                    parameter_code=record.parameter.code,
                )
            seen_identities.add(record.series_id)
            references.extend(
                await self._time_series_refs_for_record(plan, record, deadline)
            )
        return tuple(sorted(references, key=self._series_ref_sort_key))

    async def _time_series_refs_for_record(
        self,
        plan: _AvailabilityPlan,
        record: TimeSeriesMetadata,
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        units = plan.units_by_parameter.get(record.parameter, ())
        historical = plan.historical_ranges
        prediction = plan.prediction_baseline_ranges
        by_source: dict[AvailabilityCoverageSource, list[CoverageUnit]] = {}
        historical_units = tuple(
            unit for unit in units if unit.window_index is not None
        )
        requested = tuple(
            historical[(unit.segment_index, unit.window_index)]
            for unit in historical_units
        )
        observed_windows = await self._time_series_observation_windows(
            record, requested, deadline
        )
        observed_units = {
            unit for unit in historical_units if unit.window_index in observed_windows
        }
        for unit in units:
            if unit.window_index is not None:
                if unit in observed_units:
                    by_source.setdefault(
                        AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION,
                        [],
                    ).append(unit)
            else:
                baseline = prediction[unit.segment_index]
                if record.begin <= baseline.end and record.end >= baseline.start:
                    by_source.setdefault(
                        AvailabilityCoverageSource.PREDICTION_TIME_SERIES_BASELINE,
                        [],
                    ).append(unit)
        return tuple(
            SeriesRef(
                provider=ParameterProvider.WDFN_TIME_SERIES,
                station_id=record.station_id,
                parameter=record.parameter,
                channel=record.kind.value,
                series_identity=record.series_id,
                coverage_source=source,
                covered_units=tuple(sorted(source_units, key=self._coverage_sort_key)),
            )
            for source, source_units in sorted(
                by_source.items(),
                key=lambda item: item[0].value,
            )
        )

    async def _time_series_observation_windows(
        self,
        record: TimeSeriesMetadata,
        requested: tuple[AvailabilityTimeRange, ...],
        deadline: float | None,
    ) -> tuple[int, ...]:
        if not requested:
            return ()
        try:
            async with self._probe_semaphore:
                self._ensure_deadline(deadline)
                found = await self._metadata.get_time_series_observation_windows(
                    record, requested, deadline=deadline
                )
            return self._validate_observed_windows(
                found, requested, operation="get_time_series_observation_windows"
            )
        except PortError as error:
            logger.warning(
                "Time-series availability probe failed for station %s, series %s: %s",
                record.station_id,
                record.series_id,
                error,
                extra={
                    "event_name": "station_time_series_probe_failed",
                    "station_id": record.station_id,
                    "series_id": record.series_id,
                    "parameter_code": record.parameter.code,
                    "provider": error.provider,
                    "provider_operation": error.operation,
                    "provider_error": type(error).__name__,
                    "provider_status": error.status_code,
                },
            )
            raise

    async def _sample_references(
        self,
        plan: _AvailabilityPlan,
        stations: dict[str, MonitoringStation],
        parameters: tuple[ParameterKey, ...],
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        requested = set(parameters)
        references: list[SeriesRef] = []
        for station_id in sorted(stations):
            self._ensure_deadline(deadline)
            try:
                periods = await self._metadata.get_sample_periods(
                    station_id,
                    deadline=deadline,
                )
            except PortError as error:
                logger.warning(
                    "Sample periods lookup failed for station %s: %s",
                    station_id,
                    error,
                    extra={
                        "event_name": "station_sample_periods_failed",
                        "station_id": station_id,
                        "provider": error.provider,
                        "provider_operation": error.operation,
                        "provider_error": type(error).__name__,
                        "provider_status": error.status_code,
                    },
                )
                raise
            references.extend(
                await self._qualify_sample_periods(
                    plan,
                    station_id,
                    periods,
                    requested=requested,
                    deadline=deadline,
                )
            )
        return tuple(sorted(references, key=self._series_ref_sort_key))

    async def _qualify_sample_periods(
        self,
        plan: _AvailabilityPlan,
        station_id: str,
        records: tuple[SamplePeriodOfRecord, ...],
        *,
        requested: set[ParameterKey],
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        seen_identities: set[str] = set()
        references: list[SeriesRef] = []
        for record in records:
            if (
                record.station_id != station_id
                or record.provider_identity in seen_identities
            ):
                raise self._invalid_metadata(
                    "get_sample_periods",
                    station_id=record.station_id,
                    sample_identity=record.provider_identity,
                )
            seen_identities.add(record.provider_identity)
            parameter = self._sample_key_by_label.get(
                normalize_parameter_concept(record.characteristic)
            )
            if (
                parameter is None
                or parameter not in requested
            ):
                continue
            references.extend(
                await self._sample_refs_for_record(plan, record, parameter, deadline)
            )
        return tuple(sorted(references, key=self._series_ref_sort_key))

    async def _sample_refs_for_record(
        self,
        plan: _AvailabilityPlan,
        record: SamplePeriodOfRecord,
        parameter: ParameterKey,
        deadline: float | None,
    ) -> tuple[SeriesRef, ...]:
        units = plan.units_by_parameter.get(parameter, ())
        historical = plan.historical_ranges
        prediction = plan.prediction_baseline_ranges
        by_source: dict[AvailabilityCoverageSource, list[CoverageUnit]] = {}
        historical_units = tuple(
            unit for unit in units if unit.window_index is not None
        )
        requested = tuple(
            historical[(unit.segment_index, unit.window_index)]
            for unit in historical_units
        )
        observed_windows = await self._sample_observation_windows(
            record, parameter, requested, deadline
        )
        observed_units = {
            unit for unit in historical_units if unit.window_index in observed_windows
        }
        for unit in units:
            if unit.window_index is not None:
                if unit in observed_units:
                    by_source.setdefault(
                        AvailabilityCoverageSource.HISTORICAL_SAMPLE_OBSERVATION,
                        [],
                    ).append(unit)
            else:
                baseline = prediction[unit.segment_index]
                if (
                    record.result_count > 0
                    and record.activity_count > 0
                    and record.first_activity <= baseline.end.date()
                    and record.most_recent_activity >= baseline.start.date()
                ):
                    by_source.setdefault(
                        AvailabilityCoverageSource.PREDICTION_SAMPLE_BASELINE,
                        [],
                    ).append(unit)
        return tuple(
            SeriesRef(
                provider=ParameterProvider.USGS_SAMPLES,
                station_id=record.station_id,
                parameter=parameter,
                channel="discrete_samples",
                series_identity=record.provider_identity,
                coverage_source=source,
                covered_units=tuple(sorted(source_units, key=self._coverage_sort_key)),
            )
            for source, source_units in sorted(
                by_source.items(),
                key=lambda item: item[0].value,
            )
        )

    async def _sample_observation_windows(
        self,
        record: SamplePeriodOfRecord,
        parameter: ParameterKey,
        requested: tuple[AvailabilityTimeRange, ...],
        deadline: float | None,
    ) -> tuple[int, ...]:
        if not requested:
            return ()
        try:
            async with self._probe_semaphore:
                self._ensure_deadline(deadline)
                found = await self._metadata.get_sample_observation_windows(
                    record, parameter, requested, deadline=deadline
                )
            return self._validate_observed_windows(
                found, requested, operation="get_sample_observation_windows"
            )
        except PortError as error:
            logger.warning(
                "Sample availability probe failed for station %s, sample %s: %s",
                record.station_id,
                record.provider_identity,
                error,
                extra={
                    "event_name": "station_sample_probe_failed",
                    "station_id": record.station_id,
                    "sample_identity": record.provider_identity,
                    "parameter_code": parameter.code,
                    "provider": error.provider,
                    "provider_operation": error.operation,
                    "provider_error": type(error).__name__,
                    "provider_status": error.status_code,
                },
            )
            raise

    @staticmethod
    def _validate_observed_windows(
        found: object,
        requested: tuple[AvailabilityTimeRange, ...],
        *,
        operation: str,
    ) -> tuple[int, ...]:
        expected = {item.window_index for item in requested}
        if (
            type(found) is not tuple
            or any(type(index) is not int for index in found)
            or found != tuple(sorted(set(found)))
            or not set(found).issubset(expected)
        ):
            raise InvalidPortResponseError(
                "Observation probe returned invalid window indexes.",
                provider=PROVIDER,
                operation=operation,
            )
        return found

    def _build_sample_label_index(self) -> dict[str, ParameterKey]:
        matches: dict[str, set[ParameterKey]] = {}
        for capability in self._registry.capabilities:
            if ParameterProvider.USGS_SAMPLES not in (
                capability.availability_providers
            ):
                continue
            for label in {
                capability.canonical_name,
                capability.official_name,
                *capability.aliases_en,
            }:
                matches.setdefault(
                    normalize_parameter_concept(label),
                    set(),
                ).add(capability.key)
        return {
            label: next(iter(keys))
            for label, keys in matches.items()
            if len(keys) == 1
        }

    @staticmethod
    def _coverage_sort_key(unit: CoverageUnit) -> tuple[int, int, str, str]:
        return (
            unit.segment_index,
            -1 if unit.window_index is None else unit.window_index,
            unit.parameter.code_system,
            unit.parameter.code,
        )

    @staticmethod
    def _series_ref_sort_key(
        reference: SeriesRef,
    ) -> tuple[str, str, str, str, str]:
        return (
            reference.provider.value,
            reference.parameter.code_system,
            reference.parameter.code,
            reference.coverage_source.value,
            reference.series_identity,
        )

    @staticmethod
    def _invalid_metadata(
        operation: str,
        *,
        station_id: str | None = None,
        series_id: str | None = None,
        sample_identity: str | None = None,
        parameter_code: str | None = None,
        comid: str | None = None,
    ) -> InvalidPortResponseError:
        logger.warning(
            "Provider metadata identities are inconsistent in %s.",
            operation,
            extra={
                "event_name": "station_metadata_invalid",
                "provider": PROVIDER,
                "provider_operation": operation,
                "provider_error": "InvalidPortResponseError",
                "station_id": station_id,
                "series_id": series_id,
                "sample_identity": sample_identity,
                "parameter_code": parameter_code,
                "comid": comid,
            },
        )
        return InvalidPortResponseError(
            "Provider metadata identities are inconsistent.",
            provider=PROVIDER,
            operation=operation,
        )

    @staticmethod
    def _ensure_deadline(deadline: float | None) -> None:
        if (
            deadline is not None
            and asyncio.get_running_loop().time() >= deadline
        ):
            raise PortTimeoutError(
                "Station availability deadline was exhausted.",
                provider=PROVIDER,
                operation="check_availability",
            )
