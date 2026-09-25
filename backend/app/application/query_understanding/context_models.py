from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from app.application.query_understanding.models import ParameterConcept
from app.domain import (
    DomainEnum,
    DomainModel,
    GeoPoint,
    InferenceMode,
    MemoryMode,
    ModelSelection,
    NonEmptyString,
    ParameterCapabilityRegistry,
    ParameterKey,
    TimeRange,
    TimeResolution,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
    TimeResolutionSource,
    UtcDatetime,
    build_time_windows,
    ensure_unique_tuple,
)
from app.domain.time.timezone import get_iana_timezone


CONUS_SUBDIVISION_CODES = frozenset(
    {
        "US-AL",
        "US-AZ",
        "US-AR",
        "US-CA",
        "US-CO",
        "US-CT",
        "US-DE",
        "US-DC",
        "US-FL",
        "US-GA",
        "US-ID",
        "US-IL",
        "US-IN",
        "US-IA",
        "US-KS",
        "US-KY",
        "US-LA",
        "US-ME",
        "US-MD",
        "US-MA",
        "US-MI",
        "US-MN",
        "US-MS",
        "US-MO",
        "US-MT",
        "US-NE",
        "US-NV",
        "US-NH",
        "US-NJ",
        "US-NM",
        "US-NY",
        "US-NC",
        "US-ND",
        "US-OH",
        "US-OK",
        "US-OR",
        "US-PA",
        "US-RI",
        "US-SC",
        "US-SD",
        "US-TN",
        "US-TX",
        "US-UT",
        "US-VT",
        "US-VA",
        "US-WA",
        "US-WV",
        "US-WI",
        "US-WY",
    }
)
DISCHARGE_KEY = ParameterKey(code_system="usgs", code="00060")


class ClarificationSubject(DomainEnum):
    LOCATION = "location"
    DATE_RANGE = "date_range"
    PARAMETER = "parameter"
    RESOLUTION = "resolution"


class ClarificationIssue(DomainModel):
    subject: ClarificationSubject
    message: NonEmptyString


class ParsedIntentV1(DomainModel):
    location_mention: NonEmptyString | None = None
    coordinate_mention: GeoPoint | None = None
    start_date: date | None = None
    end_date: date | None = None
    parameters: tuple[ParameterConcept, ...]
    ambiguities: tuple[ClarificationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_parser_contract(self) -> Self:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if not self.parameters:
            raise ValueError("parameters must not be empty")
        ensure_unique_tuple(self.ambiguities, field_name="ambiguities")
        return self


class TargetSource(DomainEnum):
    FRONTEND = "frontend"
    QUERY = "query"


class ResolvedTarget(DomainModel):
    comid: NonEmptyString
    point: GeoPoint
    display_name: NonEmptyString | None = None
    country_code: Literal["US"]
    subdivision_code: NonEmptyString
    source: TargetSource

    @field_validator("comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        if not value.isdecimal() or value.startswith("0"):
            raise ValueError("comid must be a positive decimal string")
        return value

    @field_validator("subdivision_code")
    @classmethod
    def validate_subdivision(cls, value: str) -> str:
        if value not in CONUS_SUBDIVISION_CODES:
            raise ValueError("subdivision_code must identify CONUS or DC")
        return value


class LocalDateRange(DomainModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not follow end_date")
        return self

    @property
    def calendar_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class ParameterIntentSource(DomainEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class ResolvedParameterIntent(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    source: ParameterIntentSource
    rationale: NonEmptyString


class SupportingIntentReason(DomainEnum):
    SYSTEM_REQUIRED = "system_required"
    CAPABILITY_REQUIRED = "capability_required"


class SupportingParameterIntent(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    reason: SupportingIntentReason


class AnalysisTimeSegment(DomainModel):
    segment_index: int = Field(strict=True, ge=0)
    mode: InferenceMode
    time_range: TimeRange

    @model_validator(mode="after")
    def reject_mixed_mode(self) -> Self:
        if self.mode is InferenceMode.MIXED:
            raise ValueError("time segment mode must not be mixed")
        return self


class AnalysisContext(DomainModel):
    schema_version: Literal[1] = 1
    query: NonEmptyString
    decision_instant: UtcDatetime
    analysis_timezone: NonEmptyString
    target: ResolvedTarget
    requested_date_range: LocalDateRange
    time_range: TimeRange
    time_segments: tuple[AnalysisTimeSegment, ...]
    mode: InferenceMode
    time_resolution: TimeResolution
    time_resolution_source: TimeResolutionSource
    params_used: tuple[ResolvedParameterIntent, ...]
    supporting_params: tuple[SupportingParameterIntent, ...]
    parser_model: ModelSelection
    waterbody_model: ModelSelection
    memory_mode: MemoryMode

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @field_validator("query")
    @classmethod
    def reject_nul_query(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("query must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        zone = get_iana_timezone(self.analysis_timezone)
        today = self.decision_instant.astimezone(zone).date()
        minimum = today - timedelta(days=7)
        maximum = today + timedelta(days=7)
        requested = self.requested_date_range
        if requested.start_date < minimum or requested.end_date > maximum:
            raise ValueError("requested dates must be within local today plus or minus 7 days")

        expected_start = datetime.combine(requested.start_date, time.min, zone).astimezone(
            timezone.utc
        )
        expected_end = datetime.combine(
            requested.end_date + timedelta(days=1), time.min, zone
        ).astimezone(timezone.utc)
        if self.time_range != TimeRange(start=expected_start, end=expected_end):
            raise ValueError("time_range must exactly cover the requested local dates")

        expected_mode, expected_segments = self._expected_segments(today, zone)
        if self.mode is not expected_mode:
            raise ValueError("mode must be derived from the requested local dates")
        if self.time_segments != expected_segments:
            raise ValueError("time_segments must exactly partition time_range")

        self._validate_resolution()
        if len(build_time_windows(
            self.time_range,
            self.time_resolution,
            self.analysis_timezone,
        )) > 60:
            raise ValueError("time resolution must produce at most 60 windows")

        if not 1 <= len(self.params_used) <= 6:
            raise ValueError("params_used must contain between 1 and 6 targets")
        target_keys = tuple(item.key for item in self.params_used)
        support_keys = tuple(item.key for item in self.supporting_params)
        ensure_unique_tuple(target_keys, field_name="params_used")
        ensure_unique_tuple(support_keys, field_name="supporting_params")
        if set(target_keys) & set(support_keys):
            raise ValueError("target and supporting parameters must not overlap")
        if DISCHARGE_KEY not in set(target_keys) | set(support_keys):
            raise ValueError("analysis parameter plan must include usgs:00060 discharge")
        return self

    def validate_capability_registry(
        self,
        registry: ParameterCapabilityRegistry,
    ) -> None:
        for item in (*self.params_used, *self.supporting_params):
            capability = registry.get(item.key)
            if capability is None:
                raise ValueError(f"parameter {item.key.code_system}:{item.key.code} is not executable")
            if (
                item.canonical_name != capability.canonical_name
                or item.canonical_unit != capability.canonical_unit
            ):
                raise ValueError("parameter identity must match the capability registry")
        expected_support = {
            capability.key for capability in registry.required_support
        } - {item.key for item in self.params_used}
        if {item.key for item in self.supporting_params} != expected_support:
            raise ValueError("supporting_params must match registry requirements")

    def validate_configuration(
        self,
        configuration: AnalysisConfigurationSnapshot,
    ) -> None:
        if self.analysis_timezone != configuration.analysis_timezone:
            raise ValueError("analysis_timezone must match run configuration")
        if self.parser_model != configuration.parser_model:
            raise ValueError("parser_model must match run configuration")
        if self.waterbody_model != configuration.waterbody_model:
            raise ValueError("waterbody_model must match run configuration")
        if self.memory_mode is not configuration.memory_mode:
            raise ValueError("memory_mode must match run configuration")

        preference = configuration.time_resolution_preference
        source = configuration.time_resolution_preference_source
        if preference is TimeResolutionPreference.AUTO:
            expected_resolution = (
                TimeResolution.SIX_HOURS
                if self.requested_date_range.calendar_days <= 7
                else TimeResolution.TWELVE_HOURS
            )
            expected_source = TimeResolutionSource.AUTO
        else:
            expected_resolution = TimeResolution(preference.value)
            expected_source = (
                TimeResolutionSource.DEFAULT
                if source is TimeResolutionPreferenceSource.DEFAULT
                else TimeResolutionSource.FRONTEND
            )
        if (
            self.time_resolution is not expected_resolution
            or self.time_resolution_source is not expected_source
        ):
            raise ValueError("resolved time resolution must match run configuration")

    def _expected_segments(
        self,
        today: date,
        zone: ZoneInfo,
    ) -> tuple[InferenceMode, tuple[AnalysisTimeSegment, ...]]:
        requested = self.requested_date_range
        if requested.end_date < today:
            mode = InferenceMode.GAP_FILLING
            parts = ((InferenceMode.GAP_FILLING, self.time_range),)
        elif requested.start_date >= today:
            mode = InferenceMode.PREDICTION
            parts = ((InferenceMode.PREDICTION, self.time_range),)
        else:
            mode = InferenceMode.MIXED
            split = datetime.combine(today, time.min, zone).astimezone(timezone.utc)
            parts = (
                (
                    InferenceMode.GAP_FILLING,
                    TimeRange(start=self.time_range.start, end=split),
                ),
                (
                    InferenceMode.PREDICTION,
                    TimeRange(start=split, end=self.time_range.end),
                ),
            )
        return mode, tuple(
            AnalysisTimeSegment(segment_index=index, mode=part_mode, time_range=part)
            for index, (part_mode, part) in enumerate(parts)
        )

    def _validate_resolution(self) -> None:
        days = self.requested_date_range.calendar_days
        if self.time_resolution_source is TimeResolutionSource.DEFAULT:
            if self.time_resolution is not TimeResolution.TWELVE_HOURS:
                raise ValueError("default resolution must be 12h")
        elif self.time_resolution_source is TimeResolutionSource.AUTO:
            expected = (
                TimeResolution.SIX_HOURS
                if days <= 7
                else TimeResolution.TWELVE_HOURS
            )
            if self.time_resolution is not expected:
                raise ValueError("auto resolution must follow calendar-day rules")


class AnalysisContextResolutionStatus(DomainEnum):
    SUCCESS = "success"
    QUERY_CLARIFICATION_REQUIRED = "query_clarification_required"
    LOCATION_REQUIRED = "location_required"
    LOCATION_AMBIGUOUS = "location_ambiguous"
    LOCATION_CONFLICT = "location_conflict"
    LOCATION_OUTSIDE_SUPPORTED_REGION = "location_outside_supported_region"
    LOCATION_NOT_SUPPORTED = "location_not_supported"
    TARGET_MISMATCH = "target_mismatch"
    DATE_RANGE_REQUIRED = "date_range_required"
    DATE_RANGE_CONFLICT = "date_range_conflict"
    DATE_RANGE_OUT_OF_BOUNDS = "date_range_out_of_bounds"
    RESOLUTION_CONFLICT = "resolution_conflict"
    PARAMETER_REQUIRED = "parameter_required"
    PARAMETER_AMBIGUOUS = "parameter_ambiguous"
    PARAMETER_UNSUPPORTED = "parameter_unsupported"
    TOO_MANY_PARAMETERS = "too_many_parameters"


class AnalysisContextResolution(DomainModel):
    status: AnalysisContextResolutionStatus
    context: AnalysisContext | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        succeeded = self.status is AnalysisContextResolutionStatus.SUCCESS
        if succeeded != (self.context is not None):
            raise ValueError("only successful resolution may expose an AnalysisContext")
        return self
