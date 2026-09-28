from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import model_validator

from app.application.query_understanding.context_models import (
    AnalysisTimeSegment,
    LocalDateRange,
    ParsedIntentV1,
)
from app.application.transcripts.models import AnalysisDateRangeV1
from app.domain import (
    DomainEnum,
    DomainModel,
    InferenceMode,
    TimeRange,
    TimeResolution,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TimeResolutionSource,
    UtcDatetime,
    build_time_windows,
)
from app.domain.common import normalize_utc_datetime
from app.domain.time.timezone import get_iana_timezone


class DateResolutionStatus(DomainEnum):
    SUCCESS = "success"
    DATE_RANGE_REQUIRED = "date_range_required"
    DATE_RANGE_CONFLICT = "date_range_conflict"
    DATE_RANGE_OUT_OF_BOUNDS = "date_range_out_of_bounds"
    RESOLUTION_CONFLICT = "resolution_conflict"


class ResolvedTemporalContext(DomainModel):
    requested_date_range: LocalDateRange
    time_range: TimeRange
    time_segments: tuple[AnalysisTimeSegment, ...]
    mode: InferenceMode
    time_resolution: TimeResolution
    time_resolution_source: TimeResolutionSource


class DateResolution(DomainModel):
    status: DateResolutionStatus
    temporal: ResolvedTemporalContext | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        succeeded = self.status is DateResolutionStatus.SUCCESS
        if succeeded != (self.temporal is not None):
            raise ValueError("only successful date resolution may expose temporal facts")
        return self


class DateResolver:
    def resolve(
        self,
        frontend_date_range: AnalysisDateRangeV1 | None,
        parsed_intent: ParsedIntentV1,
        *,
        decision_instant: UtcDatetime,
        analysis_timezone: str,
        resolution_preference: TimeResolutionPreference,
        resolution_preference_source: TimeResolutionPreferenceSource,
    ) -> DateResolution:
        decision_instant = normalize_utc_datetime(decision_instant)
        parsed_range = (
            (parsed_intent.start_date, parsed_intent.end_date)
            if parsed_intent.start_date is not None
            else None
        )
        if frontend_date_range is None and parsed_range is None:
            return DateResolution(status=DateResolutionStatus.DATE_RANGE_REQUIRED)
        if frontend_date_range is not None:
            chosen = (frontend_date_range.start_date, frontend_date_range.end_date)
            if parsed_range is not None and parsed_range != chosen:
                return DateResolution(status=DateResolutionStatus.DATE_RANGE_CONFLICT)
        else:
            assert parsed_range is not None
            chosen = parsed_range
        start_date, end_date = chosen
        assert start_date is not None and end_date is not None

        zone = get_iana_timezone(analysis_timezone)
        today = decision_instant.astimezone(zone).date()
        if (
            start_date > end_date
            or start_date < today - timedelta(days=7)
            or end_date > today + timedelta(days=7)
        ):
            return DateResolution(status=DateResolutionStatus.DATE_RANGE_OUT_OF_BOUNDS)

        requested = LocalDateRange(start_date=start_date, end_date=end_date)
        utc_range = TimeRange(
            start=datetime.combine(start_date, time.min, zone).astimezone(timezone.utc),
            end=datetime.combine(
                end_date + timedelta(days=1), time.min, zone
            ).astimezone(timezone.utc),
        )
        mode, segments = self._derive_segments(requested, utc_range, today, zone)
        resolution, source = self._resolve_resolution(
            requested,
            resolution_preference,
            resolution_preference_source,
        )
        if len(build_time_windows(utc_range, resolution, analysis_timezone)) > 60:
            return DateResolution(status=DateResolutionStatus.RESOLUTION_CONFLICT)
        return DateResolution(
            status=DateResolutionStatus.SUCCESS,
            temporal=ResolvedTemporalContext(
                requested_date_range=requested,
                time_range=utc_range,
                time_segments=segments,
                mode=mode,
                time_resolution=resolution,
                time_resolution_source=source,
            ),
        )

    @staticmethod
    def _derive_segments(
        requested: LocalDateRange,
        utc_range: TimeRange,
        today: date,
        zone: ZoneInfo,
    ) -> tuple[InferenceMode, tuple[AnalysisTimeSegment, ...]]:
        if requested.end_date < today:
            mode = InferenceMode.GAP_FILLING
            parts = ((mode, utc_range),)
        elif requested.start_date >= today:
            mode = InferenceMode.PREDICTION
            parts = ((mode, utc_range),)
        else:
            mode = InferenceMode.MIXED
            split = datetime.combine(today, time.min, zone).astimezone(timezone.utc)
            parts = (
                (
                    InferenceMode.GAP_FILLING,
                    TimeRange(start=utc_range.start, end=split),
                ),
                (
                    InferenceMode.PREDICTION,
                    TimeRange(start=split, end=utc_range.end),
                ),
            )
        return mode, tuple(
            AnalysisTimeSegment(segment_index=index, mode=part_mode, time_range=part)
            for index, (part_mode, part) in enumerate(parts)
        )

    @staticmethod
    def _resolve_resolution(
        requested: LocalDateRange,
        preference: TimeResolutionPreference,
        preference_source: TimeResolutionPreferenceSource,
    ) -> tuple[TimeResolution, TimeResolutionSource]:
        if preference is TimeResolutionPreference.AUTO:
            resolution = (
                TimeResolution.SIX_HOURS
                if requested.calendar_days <= 7
                else TimeResolution.TWELVE_HOURS
            )
            return resolution, TimeResolutionSource.AUTO
        resolution = TimeResolution(preference.value)
        source = (
            TimeResolutionSource.DEFAULT
            if preference_source is TimeResolutionPreferenceSource.DEFAULT
            else TimeResolutionSource.FRONTEND
        )
        return resolution, source
