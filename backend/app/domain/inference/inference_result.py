from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.domain.attribution.analysis_detail import AnalysisDetail
from app.domain.attribution.attribution_summary import AttributionSummary
from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.evidence.evidence_chain import EvidenceChain
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.composite_risk_assessment import CompositeRiskAssessment
from app.domain.inference.mode import InferenceMode
from app.domain.inference.model_execution_summary import ModelExecutionSummary
from app.domain.inference.outcome_status import OutcomeStatus
from app.domain.inference.overall_waterbody_assessment import (
    OverallWaterbodyAssessment,
)
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
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    mode: InferenceMode
    time_range: TimeRange
    time_segments: tuple[TimeSegment, ...]
    analysis_timezone: NonEmptyString
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    requested_parameters: tuple[ParameterKey, ...]
    series: tuple[ParameterTimeSeries, ...]
    window_assessments: tuple[CompositeRiskAssessment, ...]
    overall_assessment: OverallWaterbodyAssessment
    overall_confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    overall_confidence_rationale: NonEmptyString
    attribution_summary: AttributionSummary
    analysis_detail: AnalysisDetail
    evidence_chain: EvidenceChain
    model_execution_summary: ModelExecutionSummary
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
        self._validate_assessments()
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

        segment_modes = tuple(segment.mode for segment in self.time_segments)
        if self.mode is InferenceMode.MIXED:
            if (
                segment_modes[0] is not InferenceMode.GAP_FILLING
                or segment_modes[-1] is not InferenceMode.PREDICTION
                or any(
                    previous is InferenceMode.PREDICTION
                    and current is InferenceMode.GAP_FILLING
                    for previous, current in zip(
                        segment_modes,
                        segment_modes[1:],
                    )
                )
            ):
                raise ValueError(
                    "mixed mode requires gap_filling segments followed by "
                    "prediction segments"
                )
        elif set(segment_modes) != {self.mode}:
            raise ValueError("time segment modes must match result mode")

    def _validate_series(self) -> None:
        if not self.requested_parameters:
            raise ValueError("requested_parameters must not be empty")
        ensure_unique_tuple(
            self.requested_parameters,
            field_name="requested_parameters",
        )
        if not self.series:
            raise ValueError("series must not be empty")
        parameter_keys = tuple(item.parameter.key for item in self.series)
        ensure_unique_tuple(parameter_keys, field_name="series parameter keys")
        if parameter_keys != self.requested_parameters:
            raise ValueError(
                "series parameter order must exactly match requested_parameters"
            )

        planned_windows = build_time_windows(
            self.time_range,
            self.time_resolution,
            self.analysis_timezone,
        )
        expected_bounds = tuple(
            (window.start, window.end) for window in planned_windows
        )
        all_cell_ids: list[str] = []
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
                all_cell_ids.append(point.cell_id)
                if (
                    point.outcome_status is OutcomeStatus.QUANTIFIED
                    and point.unit_id != item.parameter.canonical_unit
                ):
                    raise ValueError(
                        "quantified unit_id must match parameter canonical_unit"
                    )
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
                if point.outcome_status is OutcomeStatus.INDETERMINATE:
                    continue
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
        ensure_unique_tuple(tuple(all_cell_ids), field_name="result cell IDs")

    def _validate_assessments(self) -> None:
        planned_windows = build_time_windows(
            self.time_range,
            self.time_resolution,
            self.analysis_timezone,
        )
        if len(self.window_assessments) != len(planned_windows):
            raise ValueError(
                "window_assessments must exactly match planned windows"
            )
        ensure_unique_tuple(
            tuple(item.assessment_id for item in self.window_assessments),
            field_name="window assessment IDs",
        )

        all_cell_ids = tuple(
            point.cell_id for item in self.series for point in item.points
        )
        for index, (window, assessment) in enumerate(
            zip(planned_windows, self.window_assessments, strict=True)
        ):
            if (
                assessment.start_time != window.start
                or assessment.end_time != window.end
            ):
                raise ValueError(
                    "window_assessments must exactly match planned windows"
                )
            window_cell_ids = {
                item.points[index].cell_id for item in self.series
            }
            if assessment.total_cell_count != len(window_cell_ids):
                raise ValueError(
                    "window total_cell_count must match requested parameters"
                )
            referenced_cells = set(assessment.contributing_cell_refs) | set(
                assessment.missing_cell_refs
            )
            if not referenced_cells <= window_cell_ids:
                raise ValueError(
                    "window assessment references cells outside its window"
                )
            missing_points = (
                item.points[index]
                for item in self.series
                if item.points[index].cell_id in assessment.missing_cell_refs
            )
            if any(
                point.outcome_status is not OutcomeStatus.INDETERMINATE
                for point in missing_points
            ):
                raise ValueError("missing cell refs must identify indeterminate points")

        if self.overall_assessment.time_range != self.time_range:
            raise ValueError("overall assessment time_range must match result")
        if self.overall_assessment.total_cell_count != len(all_cell_ids):
            raise ValueError("overall total_cell_count must match result cells")
        expected_missing = tuple(
            cell_ref
            for item in self.window_assessments
            for cell_ref in item.missing_cell_refs
        )
        if self.overall_assessment.missing_cell_refs != expected_missing:
            raise ValueError(
                "overall missing_cell_refs must match window assessments"
            )
        known_window_refs = {
            item.assessment_id for item in self.window_assessments
        }
        if not set(self.overall_assessment.contributing_window_refs) <= (
            known_window_refs
        ):
            raise ValueError("overall assessment references unknown windows")
        known_nonmissing_cells = set(all_cell_ids) - set(expected_missing)
        if not set(self.overall_assessment.contributing_cell_refs) <= (
            known_nonmissing_cells
        ):
            raise ValueError("overall assessment references unknown or missing cells")
        if self.overall_confidence != self.overall_assessment.confidence:
            raise ValueError("overall_confidence must match overall assessment")
        if (
            self.overall_confidence_rationale
            != self.overall_assessment.confidence_rationale
        ):
            raise ValueError(
                "overall_confidence_rationale must match overall assessment"
            )
        if self.model_execution_summary.skipped_cells != len(expected_missing):
            raise ValueError("skipped_cells must match overall missing cells")

    def _validate_evidence_references(self) -> None:
        chain_ids = {
            reference.evidence_id for reference in self.evidence_chain.references
        }
        referenced_by: list[tuple[str, tuple[str, ...]]] = []
        for item in self.series:
            for point in item.points:
                referenced_by.append(("window", point.evidence_refs))
                referenced_by.append(
                    ("window assessment", point.assessment.supporting_refs)
                )
        for item in self.window_assessments:
            referenced_by.append(("window composite", item.evidence_refs))
            referenced_by.append(("window blocker", item.blocker_refs))
        referenced_by.append(
            ("overall assessment", self.overall_assessment.evidence_refs)
        )
        referenced_by.append(
            ("overall blocker", self.overall_assessment.blocker_refs)
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
