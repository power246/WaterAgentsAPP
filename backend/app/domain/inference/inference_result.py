from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.attribution.analysis_detail import AnalysisDetail
from app.domain.attribution.attribution_summary import AttributionSummary
from app.domain.common import DomainModel, NonEmptyString, UtcDatetime, ensure_unique_tuple
from app.domain.evidence.evidence_chain import EvidenceChain
from app.domain.inference.mode import InferenceMode
from app.domain.inference.parameter_time_series import ParameterTimeSeries
from app.domain.inference.result_source import ResultSource
from app.domain.time.time_range import TimeRange
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.domain.time.time_segment import TimeSegment
from app.domain.time.timezone import get_iana_timezone
from app.domain.time.window_builder import build_time_windows


class InferenceResult(DomainModel):
    result_id: NonEmptyString
    target_comid: NonEmptyString
    mode: InferenceMode
    time_range: TimeRange
    time_segments: tuple[TimeSegment, ...]
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    series: tuple[ParameterTimeSeries, ...]
    overall_confidence: Annotated[float, Field(ge=0, le=1)]
    attribution_summary: AttributionSummary
    analysis_detail: AnalysisDetail
    evidence_chain: EvidenceChain
    generated_at: UtcDatetime

    @field_validator("analysis_timezone")
    @classmethod
    def validate_analysis_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        self._validate_time_segments()
        self._validate_series()
        self._validate_evidence_references()
        return self

    def _validate_time_segments(self) -> None:
        if not self.time_segments:
            raise ValueError("time_segments must not be empty")
        if (
            self.time_segments[0].start != self.time_range.start
            or self.time_segments[-1].end != self.time_range.end
        ):
            raise ValueError("time_segments must exactly cover time_range")
        if any(
            previous.end != current.start
            for previous, current in zip(
                self.time_segments,
                self.time_segments[1:],
            )
        ):
            raise ValueError("time_segments must be ordered and contiguous")

        segment_modes = {segment.mode for segment in self.time_segments}
        if self.mode is InferenceMode.MIXED:
            if segment_modes != {
                InferenceMode.GAP_FILLING,
                InferenceMode.PREDICTION,
            }:
                raise ValueError(
                    "mixed mode requires gap_filling and prediction segments"
                )
        elif segment_modes != {self.mode}:
            raise ValueError("time segment modes must match result mode")

    def _validate_series(self) -> None:
        if not self.series:
            raise ValueError("series must not be empty")
        parameter_keys = tuple(item.parameter.key for item in self.series)
        ensure_unique_tuple(parameter_keys, field_name="series parameter keys")

        planned_windows = build_time_windows(
            self.time_range,
            self.time_resolution,
            self.analysis_timezone,
        )
        expected_bounds = tuple(
            (window.start, window.end) for window in planned_windows
        )
        for item in self.series:
            if item.time_resolution is not self.time_resolution:
                raise ValueError("series time_resolution must match result")
            if item.time_resolution_source is not self.time_resolution_source:
                raise ValueError("series time_resolution_source must match result")
            actual_bounds = tuple(
                (point.start_time, point.end_time) for point in item.points
            )
            if actual_bounds != expected_bounds:
                raise ValueError("series points must exactly match planned windows")
            for point in item.points:
                segment = next(
                    (
                        candidate
                        for candidate in self.time_segments
                        if candidate.start <= point.start_time
                        and point.end_time <= candidate.end
                    ),
                    None,
                )
                if segment is None:
                    raise ValueError("each window must fit within one time segment")
                if (
                    segment.mode is InferenceMode.PREDICTION
                    and point.source is not ResultSource.PREDICTED
                ):
                    raise ValueError("prediction segment requires predicted source")
                if (
                    segment.mode is InferenceMode.GAP_FILLING
                    and point.source
                    not in {ResultSource.OBSERVED, ResultSource.INFERRED}
                ):
                    raise ValueError(
                        "gap_filling segment requires observed or inferred source"
                    )

    def _validate_evidence_references(self) -> None:
        chain_ids = {
            reference.evidence_id for reference in self.evidence_chain.references
        }
        referenced_by: list[tuple[str, tuple[str, ...]]] = []
        for item in self.series:
            referenced_by.extend(
                ("window", point.evidence_refs) for point in item.points
            )
        referenced_by.extend(
            ("attribution", factor.evidence_refs)
            for factor in self.attribution_summary.primary_factors
        )
        referenced_by.extend(
            ("analysis step", step.evidence_refs)
            for step in self.analysis_detail.steps
        )
        for owner, evidence_refs in referenced_by:
            unknown = set(evidence_refs) - chain_ids
            if unknown:
                raise ValueError(
                    f"{owner} references evidence outside evidence_chain: "
                    f"{sorted(unknown)}"
                )
