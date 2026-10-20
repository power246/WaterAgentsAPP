from typing import Protocol

from app.domain.hydrology import (
    AvailabilityTimeRange,
    MonitoringLocationMetadata,
    ParameterKey,
    SamplePeriodOfRecord,
    TimeSeriesMetadata,
    UsgsParameterMetadata,
)


class StationMetadataPort(Protocol):
    """Empty results mean successful no-data responses; unavailability is an error."""

    async def get_parameter_codes(
        self,
        parameters: tuple[ParameterKey, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[UsgsParameterMetadata, ...]: ...

    async def get_monitoring_locations(
        self,
        station_ids: tuple[str, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[MonitoringLocationMetadata, ...]: ...

    async def get_time_series_metadata(
        self,
        station_ids: tuple[str, ...],
        parameters: tuple[ParameterKey, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[TimeSeriesMetadata, ...]: ...

    async def get_sample_periods(
        self,
        station_id: str,
        *,
        deadline: float | None = None,
    ) -> tuple[SamplePeriodOfRecord, ...]: ...

    async def get_time_series_observation_windows(
        self,
        series: TimeSeriesMetadata,
        requested: tuple[AvailabilityTimeRange, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]: ...

    async def get_sample_observation_windows(
        self,
        period: SamplePeriodOfRecord,
        parameter: ParameterKey,
        requested: tuple[AvailabilityTimeRange, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]: ...
