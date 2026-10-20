from datetime import timedelta

from app.application.query_understanding import AnalysisTimeSegment
from app.domain import (
    AvailabilityTimeRange,
    CoverageUnit,
    InferenceMode,
    ParameterKey,
    StationAvailabilityRequest,
    TimeRange,
    TimeResolution,
    build_time_windows,
    ensure_unique_tuple,
)

DISCHARGE_KEY = ParameterKey(code_system="usgs", code="00060")


def build_station_availability_request(
    *,
    comid: str,
    target_parameters: tuple[ParameterKey, ...],
    supporting_parameters: tuple[ParameterKey, ...],
    evidence_parameters: tuple[ParameterKey, ...] = (),
    time_segments: tuple[AnalysisTimeSegment, ...],
    time_resolution: TimeResolution,
    analysis_timezone: str,
    prediction_baseline_hours: int = 48,
) -> StationAvailabilityRequest:
    if not target_parameters:
        raise ValueError("target_parameters must not be empty")
    if (
        isinstance(prediction_baseline_hours, bool)
        or not isinstance(prediction_baseline_hours, int)
        or prediction_baseline_hours <= 0
    ):
        raise ValueError("prediction_baseline_hours must be a positive integer")
    ensure_unique_tuple(time_segments, field_name="time_segments")
    ordered_segments = tuple(
        sorted(time_segments, key=lambda segment: segment.segment_index)
    )
    if not ordered_segments:
        raise ValueError("time_segments must not be empty")
    if tuple(segment.segment_index for segment in ordered_segments) != tuple(
        range(len(ordered_segments))
    ):
        raise ValueError("time segment indexes must be contiguous from zero")

    parameters = tuple(
        sorted(
            {*target_parameters, *supporting_parameters, *evidence_parameters},
            key=lambda parameter: (parameter.code_system, parameter.code),
        )
    )
    if DISCHARGE_KEY not in parameters:
        raise ValueError("availability plan must include usgs:00060 discharge")

    windows = build_time_windows(
        TimeRange(
            start=ordered_segments[0].time_range.start,
            end=ordered_segments[-1].time_range.end,
        ),
        time_resolution,
        analysis_timezone,
    )
    historical_windows: dict[int, list[tuple[int, TimeRange]]] = {}
    for window_index, window in enumerate(windows):
        owners = (
            segment
            for segment in ordered_segments
            if window.start >= segment.time_range.start
            and window.end <= segment.time_range.end
        )
        owner = next(owners, None)
        if owner is None or next(owners, None) is not None:
            raise ValueError("availability window must belong to one segment")
        if owner.mode is InferenceMode.GAP_FILLING:
            historical_windows.setdefault(owner.segment_index, []).append(
                (window_index, window)
            )

    coverage_units: list[CoverageUnit] = []
    historical_ranges: list[AvailabilityTimeRange] = []
    prediction_ranges: list[AvailabilityTimeRange] = []
    baseline_delta = timedelta(hours=prediction_baseline_hours)
    for segment in ordered_segments:
        if segment.mode is InferenceMode.GAP_FILLING:
            for window_index, window in historical_windows[segment.segment_index]:
                historical_ranges.append(
                    AvailabilityTimeRange(
                        segment_index=segment.segment_index,
                        window_index=window_index,
                        start=window.start,
                        end=window.end,
                    )
                )
                coverage_units.extend(
                    CoverageUnit(
                        parameter=parameter,
                        segment_index=segment.segment_index,
                        window_index=window_index,
                    )
                    for parameter in parameters
                )
        elif segment.mode is InferenceMode.PREDICTION:
            coverage_units.extend(
                CoverageUnit(parameter=parameter, segment_index=segment.segment_index)
                for parameter in parameters
            )
            prediction_ranges.append(
                AvailabilityTimeRange(
                    segment_index=segment.segment_index,
                    start=segment.time_range.start - baseline_delta,
                    end=segment.time_range.start,
                )
            )
        else:
            raise ValueError("availability segments must not use mixed mode")

    return StationAvailabilityRequest(
        comid=comid,
        coverage_units=tuple(coverage_units),
        historical_ranges=tuple(historical_ranges),
        prediction_baseline_ranges=tuple(prediction_ranges),
    )
