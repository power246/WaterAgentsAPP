from datetime import timedelta

from app.application.query_understanding import AnalysisTimeSegment
from app.domain import (
    AvailabilityTimeRange,
    CoverageUnit,
    InferenceMode,
    ParameterKey,
    StationAvailabilityRequest,
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

    coverage_units = tuple(
        CoverageUnit(parameter=parameter, segment_index=segment.segment_index)
        for segment in ordered_segments
        for parameter in parameters
    )
    historical_ranges: list[AvailabilityTimeRange] = []
    prediction_ranges: list[AvailabilityTimeRange] = []
    baseline_delta = timedelta(hours=prediction_baseline_hours)
    for segment in ordered_segments:
        if segment.mode is InferenceMode.GAP_FILLING:
            historical_ranges.append(
                AvailabilityTimeRange(
                    segment_index=segment.segment_index,
                    start=segment.time_range.start,
                    end=segment.time_range.end,
                )
            )
        elif segment.mode is InferenceMode.PREDICTION:
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
        coverage_units=coverage_units,
        historical_ranges=tuple(historical_ranges),
        prediction_baseline_ranges=tuple(prediction_ranges),
    )
