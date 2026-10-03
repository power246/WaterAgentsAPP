from __future__ import annotations

from collections.abc import Iterable

from app.application.reasoning.draft_models import (
    ReasoningCellDraft,
    ValidatedOverallSynthesis,
    ValidatedReasoningSet,
)
from app.application.reasoning.errors import InferenceAssemblyError
from app.application.reasoning.result_models import ResultSkeleton
from app.domain import (
    AnalysisDetail,
    AnalysisStep,
    AssessmentAvailability,
    AttributionFactor,
    AttributionSummary,
    CompositeRiskAssessment,
    EvidenceChain,
    EvidenceReference,
    InferenceResult,
    ModelExecutionSummary,
    OutcomeStatus,
    OverallWaterbodyAssessment,
    ParameterCapabilityRegistry,
    ParameterTimeSeries,
    RiskLevel,
    RiskOrTrendAssessment,
    TimeWindowResult,
    WaterQualityParameter,
)


_FIXED_RISK_RATIONALE = "No supported water-quality judgment is available."
_FIXED_CONFIDENCE_RATIONALE = (
    "Confidence is zero because no requested cell has a determinate outcome."
)


def build_parameter_definitions(
    keys,
    registry: ParameterCapabilityRegistry,
) -> tuple[WaterQualityParameter, ...]:
    definitions = []
    for key in keys:
        capability = registry.get(key)
        if capability is None:
            raise InferenceAssemblyError()
        definitions.append(
            WaterQualityParameter(
                key=key,
                name=capability.canonical_name,
                canonical_unit=capability.canonical_unit,
                category=capability.official_group,
            )
        )
    return tuple(definitions)


def assemble_inference_result(
    skeleton: ResultSkeleton,
    *,
    reasoning: ValidatedReasoningSet | None,
    synthesis: ValidatedOverallSynthesis | None,
    parameter_definitions: tuple[WaterQualityParameter, ...],
    evidence_references: tuple[EvidenceReference, ...],
    model_execution_summary: ModelExecutionSummary,
) -> InferenceResult:
    """Merge only sealed facts and validated Agent output into the V1 result."""

    try:
        return _assemble(
            skeleton,
            reasoning=reasoning,
            synthesis=synthesis,
            parameter_definitions=parameter_definitions,
            evidence_references=evidence_references,
            model_execution_summary=model_execution_summary,
        )
    except InferenceAssemblyError:
        raise
    except (KeyError, TypeError, ValueError):
        raise InferenceAssemblyError() from None


def _assemble(
    skeleton: ResultSkeleton,
    *,
    reasoning: ValidatedReasoningSet | None,
    synthesis: ValidatedOverallSynthesis | None,
    parameter_definitions: tuple[WaterQualityParameter, ...],
    evidence_references: tuple[EvidenceReference, ...],
    model_execution_summary: ModelExecutionSummary,
) -> InferenceResult:
    supported_ids = tuple(scope.cell_id for scope in skeleton.supported_scopes)
    if bool(supported_ids) != (reasoning is not None and synthesis is not None):
        raise InferenceAssemblyError()
    if not supported_ids and (reasoning is not None or synthesis is not None):
        raise InferenceAssemblyError()

    parameters = tuple(item.key for item in parameter_definitions)
    if parameters != skeleton.requested_parameters:
        raise InferenceAssemblyError()
    reference_ids = tuple(item.evidence_id for item in evidence_references)
    if reference_ids != skeleton.evidence_allowlist:
        raise InferenceAssemblyError()

    drafts = (
        tuple(cell for batch in reasoning.batches for cell in batch.cells)
        if reasoning is not None
        else ()
    )
    if tuple(cell.cell_id for cell in drafts) != supported_ids:
        raise InferenceAssemblyError()
    draft_by_id = {cell.cell_id: cell for cell in drafts}
    fixed_by_id = {
        cell.result.cell_id: cell for cell in skeleton.fixed_cells
    }
    points_by_id = {
        cell_id: (
            _point_from_draft(draft_by_id[cell_id], skeleton)
            if cell_id in draft_by_id
            else fixed_by_id[cell_id].result
        )
        for cell_id in skeleton.cell_order
    }

    synthesis_series = (
        {item.parameter: item for item in synthesis.series_assessments}
        if synthesis is not None
        else {}
    )
    series = tuple(
        _series(
            skeleton,
            parameter,
            definition,
            points_by_id,
            synthesis_series.get(parameter),
        )
        for parameter, definition in zip(
            skeleton.requested_parameters,
            parameter_definitions,
            strict=True,
        )
    )

    synthesis_windows = (
        {item.window_id: item for item in synthesis.window_assessments}
        if synthesis is not None
        else {}
    )
    window_assessments = tuple(
        _window_assessment(
            skeleton,
            window_index,
            points_by_id,
            fixed_by_id,
            synthesis_windows.get(window.window_id),
        )
        for window_index, window in enumerate(skeleton.windows)
    )
    all_points = tuple(
        points_by_id[cell_id] for cell_id in skeleton.cell_order
    )
    assessed_cells = tuple(
        point.cell_id
        for point in all_points
        if point.outcome_status is not OutcomeStatus.INDETERMINATE
    )
    missing_cells = tuple(
        cell_id
        for assessment in window_assessments
        for cell_id in assessment.missing_cell_refs
    )
    blocker_refs = _ordered_evidence_refs(
        skeleton,
        (
            ref
            for fixed in skeleton.fixed_cells
            for ref in fixed.blocker_refs
        ),
    )
    overall_draft = synthesis.overall if synthesis is not None else None
    overall_evidence = _ordered_evidence_refs(
        skeleton,
        (
            *(overall_draft.evidence_refs if overall_draft is not None else ()),
            *(ref for point in all_points for ref in point.evidence_refs),
            *blocker_refs,
        ),
    )
    overall_assessment = OverallWaterbodyAssessment(
        assessment_id="overall-range",
        time_range=skeleton.time_range,
        risk_level=(
            overall_draft.risk_level
            if overall_draft is not None
            else RiskLevel.INDETERMINATE
        ),
        risk_rationale=(
            overall_draft.risk_rationale
            if overall_draft is not None
            else _FIXED_RISK_RATIONALE
        ),
        confidence=(
            overall_draft.confidence if overall_draft is not None else 0.0
        ),
        confidence_rationale=(
            overall_draft.confidence_rationale
            if overall_draft is not None
            else _FIXED_CONFIDENCE_RATIONALE
        ),
        availability=_availability(len(assessed_cells), len(all_points)),
        assessed_cell_count=len(assessed_cells),
        total_cell_count=len(all_points),
        contributing_window_refs=tuple(
            item.assessment_id
            for item in window_assessments
            if item.assessed_cell_count
        ),
        contributing_cell_refs=assessed_cells,
        missing_cell_refs=missing_cells,
        blocker_refs=blocker_refs,
        evidence_refs=overall_evidence,
    )
    analysis_detail = (
        _analysis_detail(synthesis) if synthesis is not None else _fixed_analysis()
    )
    attribution_summary = (
        _attribution_summary(synthesis)
        if synthesis is not None
        else AttributionSummary(
            summary="No determinate result is available for attribution.",
            primary_factors=(),
        )
    )
    execution = model_execution_summary.model_copy(
        update={"skipped_cells": len(missing_cells)}
    )
    return InferenceResult(
        result_id=skeleton.result_id,
        run_id=skeleton.run_id,
        session_id=skeleton.session_id,
        target_comid=skeleton.target_comid,
        mode=skeleton.mode,
        time_range=skeleton.time_range,
        time_segments=skeleton.time_segments,
        analysis_timezone=skeleton.analysis_timezone,
        time_resolution=skeleton.time_resolution,
        time_resolution_source=skeleton.time_resolution_source,
        requested_parameters=skeleton.requested_parameters,
        series=series,
        window_assessments=window_assessments,
        overall_assessment=overall_assessment,
        overall_confidence=overall_assessment.confidence,
        overall_confidence_rationale=overall_assessment.confidence_rationale,
        attribution_summary=attribution_summary,
        analysis_detail=analysis_detail,
        evidence_chain=EvidenceChain(references=evidence_references),
        model_execution_summary=execution,
        generated_at=skeleton.generated_at,
    )


def _point_from_draft(
    draft: ReasoningCellDraft,
    skeleton: ResultSkeleton,
) -> TimeWindowResult:
    scope = next(
        item for item in skeleton.supported_scopes if item.cell_id == draft.cell_id
    )
    window = next(
        item for item in skeleton.windows if item.window_id == scope.window_id
    )
    return TimeWindowResult(
        cell_id=draft.cell_id,
        start_time=window.start_time,
        end_time=window.end_time,
        outcome_status=draft.outcome_status,
        representative_value=draft.representative_value,
        unit_id=draft.target_unit_id,
        assessment=RiskOrTrendAssessment(
            summary=draft.explanation,
            trend=draft.trend,
            risk_level=draft.risk_level,
            risk_rationale=draft.risk_rationale,
            reason_codes=draft.reason_codes,
            supporting_refs=draft.evidence_refs,
        ),
        source=draft.source,
        confidence=draft.confidence,
        confidence_rationale=draft.confidence_rationale,
        aggregation_method=draft.aggregation_method,
        evidence_refs=draft.evidence_refs,
        quality_flags=draft.quality_flags,
    )


def _series(
    skeleton: ResultSkeleton,
    parameter,
    definition: WaterQualityParameter,
    points_by_id: dict[str, TimeWindowResult],
    draft,
) -> ParameterTimeSeries:
    prefix = f"cell:{parameter.code_system}:{parameter.code}:"
    cell_ids = tuple(
        cell_id for cell_id in skeleton.cell_order if cell_id.startswith(prefix)
    )
    return ParameterTimeSeries(
        parameter=definition,
        time_resolution=skeleton.time_resolution,
        time_resolution_source=skeleton.time_resolution_source,
        points=tuple(points_by_id[cell_id] for cell_id in cell_ids),
        confidence=draft.confidence if draft is not None else 0.0,
        confidence_rationale=(
            draft.confidence_rationale
            if draft is not None
            else _FIXED_CONFIDENCE_RATIONALE
        ),
        contributing_cell_refs=cell_ids,
    )


def _window_assessment(
    skeleton: ResultSkeleton,
    window_index: int,
    points_by_id: dict[str, TimeWindowResult],
    fixed_by_id: dict[str, object],
    draft,
) -> CompositeRiskAssessment:
    window = skeleton.windows[window_index]
    cell_ids = tuple(
        f"cell:{parameter.code_system}:{parameter.code}:{window.window_id}"
        for parameter in skeleton.requested_parameters
    )
    assessed = tuple(
        cell_id
        for cell_id in cell_ids
        if points_by_id[cell_id].outcome_status is not OutcomeStatus.INDETERMINATE
    )
    missing = tuple(cell_id for cell_id in cell_ids if cell_id not in set(assessed))
    blockers = _ordered_evidence_refs(
        skeleton,
        (
            ref
            for cell_id in cell_ids
            if cell_id in fixed_by_id
            for ref in fixed_by_id[cell_id].blocker_refs
        ),
    )
    evidence = _ordered_evidence_refs(
        skeleton,
        (
            *(draft.evidence_refs if draft is not None else ()),
            *(
                ref
                for cell_id in cell_ids
                for ref in points_by_id[cell_id].evidence_refs
            ),
            *blockers,
        ),
    )
    return CompositeRiskAssessment(
        assessment_id=f"overall-window:{window.window_id}",
        start_time=window.start_time,
        end_time=window.end_time,
        risk_level=(draft.risk_level if draft is not None else RiskLevel.INDETERMINATE),
        risk_rationale=(
            draft.risk_rationale if draft is not None else _FIXED_RISK_RATIONALE
        ),
        confidence=draft.confidence if draft is not None else 0.0,
        confidence_rationale=(
            draft.confidence_rationale
            if draft is not None
            else _FIXED_CONFIDENCE_RATIONALE
        ),
        availability=_availability(len(assessed), len(cell_ids)),
        assessed_cell_count=len(assessed),
        total_cell_count=len(cell_ids),
        contributing_cell_refs=assessed,
        missing_cell_refs=missing,
        blocker_refs=blockers,
        evidence_refs=evidence,
    )


def _analysis_detail(synthesis: ValidatedOverallSynthesis) -> AnalysisDetail:
    return AnalysisDetail(
        summary=synthesis.analysis_detail.summary,
        steps=tuple(
            AnalysisStep(
                type=step.type,
                title=step.title,
                content=step.content,
                evidence_refs=step.evidence_refs,
                result_cell_refs=step.result_cell_refs,
            )
            for step in synthesis.analysis_detail.steps
        ),
    )


def _fixed_analysis() -> AnalysisDetail:
    return AnalysisDetail(
        summary=(
            "The complete requested grid was assembled as indeterminate from "
            "sealed evidence facts."
        ),
        steps=(),
    )


def _attribution_summary(
    synthesis: ValidatedOverallSynthesis,
) -> AttributionSummary:
    return AttributionSummary(
        summary=synthesis.attribution_summary.summary,
        primary_factors=tuple(
            AttributionFactor(
                factor=factor.factor,
                direction=factor.direction,
                magnitude=factor.magnitude,
                basis=factor.basis,
                explanation=factor.explanation,
                evidence_refs=factor.evidence_refs,
                result_cell_refs=factor.result_cell_refs,
            )
            for factor in synthesis.attribution_summary.primary_factors
        ),
    )


def _availability(assessed: int, total: int) -> AssessmentAvailability:
    if assessed == 0:
        return AssessmentAvailability.INDETERMINATE
    if assessed == total:
        return AssessmentAvailability.COMPLETE
    return AssessmentAvailability.PARTIAL


def _ordered_evidence_refs(
    skeleton: ResultSkeleton,
    refs: Iterable[str],
) -> tuple[str, ...]:
    selected = set(refs)
    return tuple(ref for ref in skeleton.evidence_allowlist if ref in selected)
