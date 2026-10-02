from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain import (
    AggregationMethod,
    AnalysisDetail,
    AnalysisStep,
    AssessmentAvailability,
    AttributionBasis,
    AttributionDirection,
    AttributionFactor,
    AttributionMagnitude,
    AttributionSummary,
    CollectionFactPayload,
    CollectionStatus,
    CompositeRiskAssessment,
    CoverageStatus,
    EvidenceApplicability,
    EvidenceChain,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceResultEligibility,
    EvidenceReference,
    IndeterminateReason,
    InferenceMode,
    InferenceResult,
    ModelExecutionSummary,
    ModelSelection,
    ModelUsageSummary,
    OutcomeStatus,
    OverallWaterbodyAssessment,
    ParameterKey,
    ParameterTimeSeries,
    Provenance,
    ResultSource,
    RiskLevel,
    RiskOrTrendAssessment,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    TimeSegment,
    TimeWindowResult,
    TrendDirection,
    WaterQualityParameter,
    build_time_windows,
)

ANALYSIS_TIMEZONE = "America/Chicago"
RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")


def local_day(
    year: int = 2026,
    month: int = 2,
    day: int = 1,
) -> TimeRange:
    zone = ZoneInfo(ANALYSIS_TIMEZONE)
    start = datetime(year, month, day, tzinfo=zone)
    return TimeRange(start=start, end=start + timedelta(days=1))


def temperature_parameter(code_system: str = "usgs") -> WaterQualityParameter:
    return WaterQualityParameter(
        key=ParameterKey(code_system=code_system, code="00010"),
        name="Water temperature",
        canonical_unit="degC",
        category="physical",
    )


def assessment_for(
    status: OutcomeStatus,
    evidence_refs: tuple[str, ...] = ("evidence-empty",),
) -> RiskOrTrendAssessment:
    if status is OutcomeStatus.QUANTIFIED:
        return RiskOrTrendAssessment(
            summary="No identified temperature risk",
            trend=TrendDirection.STABLE,
            risk_level=RiskLevel.NONE,
            risk_rationale="The cited value supports no identified risk.",
            supporting_refs=evidence_refs,
        )
    if status is OutcomeStatus.DIRECTIONAL:
        return RiskOrTrendAssessment(
            summary="Temperature is rising",
            trend=TrendDirection.RISING,
            risk_level=RiskLevel.INDETERMINATE,
            risk_rationale="The cited trend is clear but its risk is uncertain.",
            supporting_refs=evidence_refs,
        )
    return RiskOrTrendAssessment(
        summary="No usable station observation",
        trend=None,
        risk_level=RiskLevel.INDETERMINATE,
        risk_rationale="The required evidence is insufficient.",
        reason_codes=(IndeterminateReason.INSUFFICIENT_EVIDENCE,),
        supporting_refs=evidence_refs,
    )


def make_point(
    window: TimeRange,
    *,
    cell_id: str | None = None,
    outcome_status: OutcomeStatus = OutcomeStatus.INDETERMINATE,
    source: ResultSource | None = None,
    evidence_refs: tuple[str, ...] = ("evidence-empty",),
    confidence: float | None = None,
    assessment: RiskOrTrendAssessment | None = None,
) -> TimeWindowResult:
    effective_source = source
    if outcome_status is not OutcomeStatus.INDETERMINATE and source is None:
        effective_source = ResultSource.INFERRED
    effective_confidence = confidence
    if effective_confidence is None:
        effective_confidence = (
            0 if outcome_status is OutcomeStatus.INDETERMINATE else 0.5
        )
    effective_assessment = assessment or assessment_for(
        outcome_status,
        evidence_refs,
    )
    return TimeWindowResult(
        cell_id=cell_id or f"cell:default:{int(window.start.timestamp())}",
        start_time=window.start,
        end_time=window.end,
        outcome_status=outcome_status,
        representative_value=(
            "7.4" if outcome_status is OutcomeStatus.QUANTIFIED else None
        ),
        unit_id="degC" if outcome_status is OutcomeStatus.QUANTIFIED else None,
        assessment=effective_assessment,
        source=effective_source,
        confidence=effective_confidence,
        confidence_rationale=(
            "The directness and coverage support this confidence."
            if outcome_status is not OutcomeStatus.INDETERMINATE
            else "No supported water-quality judgment is available."
        ),
        aggregation_method=(
            AggregationMethod.MEAN
            if outcome_status is OutcomeStatus.QUANTIFIED
            and effective_source is not ResultSource.OBSERVED
            else None
        ),
        evidence_refs=evidence_refs,
    )


def make_series(
    time_range: TimeRange,
    *,
    resolution: TimeResolution = TimeResolution.TWELVE_HOURS,
    resolution_source: TimeResolutionSource = TimeResolutionSource.DEFAULT,
    point_sources: tuple[ResultSource, ...] | None = None,
    point_statuses: tuple[OutcomeStatus, ...] | None = None,
    evidence_refs: tuple[tuple[str, ...], ...] | None = None,
    parameter: WaterQualityParameter | None = None,
) -> ParameterTimeSeries:
    windows = build_time_windows(time_range, resolution, ANALYSIS_TIMEZONE)
    sources: tuple[ResultSource | None, ...] = point_sources or (None,) * len(windows)
    statuses = point_statuses or (
        (OutcomeStatus.QUANTIFIED,) * len(windows)
        if point_sources is not None
        else (OutcomeStatus.INDETERMINATE,) * len(windows)
    )
    refs = evidence_refs or (("evidence-empty",),) * len(windows)
    selected_parameter = parameter or temperature_parameter()
    points = tuple(
        make_point(
            window,
            cell_id=(
                f"cell:{selected_parameter.key.code_system}:"
                f"{selected_parameter.key.code}:window-{index:04d}"
            ),
            outcome_status=status,
            source=source,
            evidence_refs=point_refs,
        )
        for index, (window, status, source, point_refs) in enumerate(
            zip(windows, statuses, sources, refs, strict=True),
            start=1,
        )
    )
    fixed = all(
        IndeterminateReason.INSUFFICIENT_EVIDENCE
        in point.assessment.reason_codes
        for point in points
    )
    return ParameterTimeSeries(
        parameter=selected_parameter,
        time_resolution=resolution,
        time_resolution_source=resolution_source,
        points=points,
        confidence=0 if fixed else 0.5,
        confidence_rationale=(
            "All planned cells are fixed as insufficient evidence."
            if fixed
            else "The cited cells support the series judgment."
        ),
        contributing_cell_refs=tuple(point.cell_id for point in points),
    )


def make_chain(*evidence_ids: str) -> EvidenceChain:
    return EvidenceChain(
        references=tuple(
            EvidenceReference(
                evidence_id=evidence_id,
                role="supporting",
                summary=None,
            )
            for evidence_id in evidence_ids
        )
    )


def unique_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(refs))


def is_fixed_point(point: TimeWindowResult) -> bool:
    return (
        IndeterminateReason.INSUFFICIENT_EVIDENCE
        in point.assessment.reason_codes
    )


def make_window_assessments(
    series: tuple[ParameterTimeSeries, ...],
) -> tuple[CompositeRiskAssessment, ...]:
    assessments: list[CompositeRiskAssessment] = []
    for index in range(len(series[0].points)):
        points = tuple(item.points[index] for item in series)
        missing = tuple(point.cell_id for point in points if is_fixed_point(point))
        contributing = tuple(
            point.cell_id for point in points if not is_fixed_point(point)
        )
        evidence_refs = unique_refs(
            tuple(ref for point in points for ref in point.evidence_refs)
        )
        if not contributing:
            availability = AssessmentAvailability.INDETERMINATE
        elif missing:
            availability = AssessmentAvailability.PARTIAL
        else:
            availability = AssessmentAvailability.COMPLETE
        point = points[0]
        assessments.append(
            CompositeRiskAssessment(
                assessment_id=f"overall-window:{index + 1:04d}",
                start_time=point.start_time,
                end_time=point.end_time,
                risk_level=(
                    RiskLevel.NONE
                    if contributing
                    else RiskLevel.INDETERMINATE
                ),
                risk_rationale=(
                    "The cited cells support no identified composite risk."
                    if contributing
                    else "No requested parameter can be assessed."
                ),
                confidence=0.5 if contributing else 0,
                confidence_rationale=(
                    "The cited cell results support this judgment."
                    if contributing
                    else "No supported water-quality judgment is available."
                ),
                availability=availability,
                assessed_cell_count=len(contributing),
                total_cell_count=len(points),
                contributing_cell_refs=contributing,
                missing_cell_refs=missing,
                blocker_refs=(),
                evidence_refs=evidence_refs,
            )
        )
    return tuple(assessments)


def make_overall_assessment(
    time_range: TimeRange,
    series: tuple[ParameterTimeSeries, ...],
    window_assessments: tuple[CompositeRiskAssessment, ...],
) -> OverallWaterbodyAssessment:
    all_points = tuple(point for item in series for point in item.points)
    missing = tuple(
        ref
        for assessment in window_assessments
        for ref in assessment.missing_cell_refs
    )
    missing_set = set(missing)
    contributing = tuple(
        point.cell_id for point in all_points if point.cell_id not in missing_set
    )
    evidence_refs = unique_refs(
        tuple(ref for point in all_points for ref in point.evidence_refs)
    )
    if not contributing:
        availability = AssessmentAvailability.INDETERMINATE
    elif missing:
        availability = AssessmentAvailability.PARTIAL
    else:
        availability = AssessmentAvailability.COMPLETE
    return OverallWaterbodyAssessment(
        assessment_id="overall-range",
        time_range=time_range,
        risk_level=(RiskLevel.NONE if contributing else RiskLevel.INDETERMINATE),
        risk_rationale=(
            "The cited results support no identified range risk."
            if contributing
            else "No requested cell can be assessed."
        ),
        confidence=0.5 if contributing else 0,
        confidence_rationale=(
            "The cited result coverage supports this judgment."
            if contributing
            else "No supported water-quality judgment is available."
        ),
        availability=availability,
        assessed_cell_count=len(contributing),
        total_cell_count=len(all_points),
        contributing_window_refs=tuple(
            item.assessment_id
            for item in window_assessments
            if item.assessed_cell_count
        ),
        contributing_cell_refs=contributing,
        missing_cell_refs=missing,
        blocker_refs=(),
        evidence_refs=evidence_refs,
    )


def make_result(
    *,
    time_range: TimeRange | None = None,
    mode: InferenceMode = InferenceMode.GAP_FILLING,
    time_segments: tuple[TimeSegment, ...] | None = None,
    resolution: TimeResolution = TimeResolution.TWELVE_HOURS,
    resolution_source: TimeResolutionSource = TimeResolutionSource.DEFAULT,
    series: tuple[ParameterTimeSeries, ...] | None = None,
    attribution_summary: AttributionSummary | None = None,
    analysis_detail: AnalysisDetail | None = None,
    evidence_chain: EvidenceChain | None = None,
    overall_confidence: float | None = None,
    window_assessments: tuple[CompositeRiskAssessment, ...] | None = None,
    overall_assessment: OverallWaterbodyAssessment | None = None,
    model_execution_summary: ModelExecutionSummary | None = None,
) -> InferenceResult:
    requested = time_range or local_day()
    segments = time_segments
    if segments is None:
        segments = (
            TimeSegment(start=requested.start, end=requested.end, mode=mode),
        )
    result_series = series
    if result_series is None:
        result_series = (
            make_series(
                requested,
                resolution=resolution,
                resolution_source=resolution_source,
            ),
        )
    result_window_assessments = window_assessments or make_window_assessments(
        result_series
    )
    result_overall = overall_assessment or make_overall_assessment(
        requested,
        result_series,
        result_window_assessments,
    )
    confidence = (
        result_overall.confidence
        if overall_confidence is None
        else overall_confidence
    )
    missing_count = len(result_overall.missing_cell_refs)
    return InferenceResult(
        result_id="result-1",
        run_id=RUN_ID,
        session_id=SESSION_ID,
        target_comid="001234",
        mode=mode,
        time_range=requested,
        time_segments=segments,
        analysis_timezone=ANALYSIS_TIMEZONE,
        time_resolution=resolution,
        time_resolution_source=resolution_source,
        requested_parameters=tuple(item.parameter.key for item in result_series),
        series=result_series,
        window_assessments=result_window_assessments,
        overall_assessment=result_overall,
        overall_confidence=confidence,
        overall_confidence_rationale=result_overall.confidence_rationale,
        attribution_summary=attribution_summary
        or AttributionSummary(summary="Insufficient attribution evidence"),
        analysis_detail=analysis_detail
        or AnalysisDetail(summary="Analysis complete", steps=()),
        evidence_chain=evidence_chain or make_chain("evidence-empty"),
        model_execution_summary=model_execution_summary
        or ModelExecutionSummary(
            model=ModelSelection(provider="openai", model="m6-fixture"),
            planning_calls=0,
            tool_calls=0,
            reasoning_batches=0,
            repair_calls=0,
            synthesis_calls=0,
            skipped_cells=missing_count,
            usage=ModelUsageSummary(input_tokens=0, output_tokens=0),
        ),
        generated_at=datetime(2026, 2, 2, 12, tzinfo=timezone.utc),
    )


def make_partial_result() -> InferenceResult:
    requested = local_day()
    windows = build_time_windows(
        requested,
        TimeResolution.TWELVE_HOURS,
        ANALYSIS_TIMEZONE,
    )
    supported = make_series(
        requested,
        point_sources=(ResultSource.INFERRED,) * len(windows),
    )
    missing = make_series(
        requested,
        parameter=temperature_parameter(code_system="epa"),
    )
    return make_result(
        time_range=requested,
        series=(supported, missing),
    )


def assert_json_round_trip(model) -> None:
    assert type(model).model_validate_json(model.model_dump_json()) == model


def test_result_enums_have_stable_values() -> None:
    assert [status.value for status in OutcomeStatus] == [
        "quantified",
        "directional",
        "indeterminate",
    ]
    assert [source.value for source in ResultSource] == [
        "observed",
        "inferred",
        "predicted",
    ]
    assert [method.value for method in AggregationMethod] == [
        "mean",
        "median",
        "min",
        "max",
        "sum",
        "p90",
        "latest",
    ]
    assert [trend.value for trend in TrendDirection] == [
        "rising",
        "falling",
        "stable",
        "uncertain",
    ]
    assert [risk.value for risk in RiskLevel] == [
        "none",
        "low",
        "medium",
        "high",
        "indeterminate",
    ]
    assert [availability.value for availability in AssessmentAvailability] == [
        "complete",
        "partial",
        "indeterminate",
    ]
    assert [reason.value for reason in IndeterminateReason] == [
        "insufficient_evidence",
        "no_station_path",
        "parameter_unobserved",
        "invalid_observations",
        "source_unavailable",
        "no_applicable_evidence",
    ]


def test_quantified_accepts_decimal_string_with_assessment() -> None:
    point = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
    )

    assert point.representative_value == "7.4"
    assert_json_round_trip(point)


def test_every_outcome_requires_assessment() -> None:
    values = make_point(local_day()).model_dump()
    values.pop("assessment")

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


def test_directional_accepts_a_definite_trend_or_risk() -> None:
    point = make_point(
        local_day(),
        outcome_status=OutcomeStatus.DIRECTIONAL,
        assessment=RiskOrTrendAssessment(
            summary="Risk remains high",
            trend=TrendDirection.UNCERTAIN,
            risk_level=RiskLevel.HIGH,
            risk_rationale="The cited evidence supports a high risk.",
            supporting_refs=("evidence-empty",),
        ),
    )

    assert point.representative_value is None
    assert_json_round_trip(point)


@pytest.mark.parametrize(
    ("representative_value", "assessment"),
    [
        ("7.4", assessment_for(OutcomeStatus.DIRECTIONAL)),
        (None, None),
        (
            None,
            RiskOrTrendAssessment(
                summary="Still uncertain",
                trend=TrendDirection.UNCERTAIN,
                risk_level=RiskLevel.INDETERMINATE,
                risk_rationale="The cited evidence remains inconclusive.",
                supporting_refs=("evidence-empty",),
            ),
        ),
        (
            None,
            RiskOrTrendAssessment(
                summary="Invalid reason placement",
                trend=TrendDirection.RISING,
                risk_level=RiskLevel.INDETERMINATE,
                risk_rationale="The cited trend is clear but risk is uncertain.",
                reason_codes=(IndeterminateReason.NO_APPLICABLE_EVIDENCE,),
                supporting_refs=("evidence-empty",),
            ),
        ),
    ],
)
def test_directional_rejects_illegal_field_combinations(
    representative_value: str | None,
    assessment: RiskOrTrendAssessment | None,
) -> None:
    values = make_point(
        local_day(),
        outcome_status=OutcomeStatus.DIRECTIONAL,
    ).model_dump()
    values["representative_value"] = representative_value
    values["assessment"] = assessment

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


@pytest.mark.parametrize(
    ("representative_value", "assessment"),
    [
        ("7.4", assessment_for(OutcomeStatus.INDETERMINATE)),
        (None, None),
        (
            None,
            RiskOrTrendAssessment(
                summary="No structured reason",
                trend=None,
                risk_level=RiskLevel.INDETERMINATE,
                risk_rationale="The cited evidence remains inconclusive.",
                supporting_refs=("evidence-empty",),
            ),
        ),
    ],
)
def test_indeterminate_rejects_illegal_field_combinations(
    representative_value: str | None,
    assessment: RiskOrTrendAssessment | None,
) -> None:
    values = make_point(local_day()).model_dump()
    values["representative_value"] = representative_value
    values["assessment"] = assessment

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


def test_quantified_requires_a_canonical_decimal_compatible_value() -> None:
    valid = make_point(local_day(), outcome_status=OutcomeStatus.QUANTIFIED)

    for value in (None, 7, 7.4, float("nan"), float("inf"), True):
        values = valid.model_dump()
        values["representative_value"] = value
        with pytest.raises(ValidationError):
            TimeWindowResult.model_validate(values)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("001.2300", "1.23"), ("0", "0"), ("-0.000", "0")],
)
def test_quantified_value_normalizes_to_canonical_decimal_string(
    raw: str,
    canonical: str,
) -> None:
    values = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
    ).model_dump()
    values["representative_value"] = raw

    assert TimeWindowResult.model_validate(values).representative_value == canonical


@pytest.mark.parametrize("field_name", ["risk_rationale", "supporting_refs"])
def test_risk_assessment_requires_rationale_and_supporting_refs(
    field_name: str,
) -> None:
    values = assessment_for(OutcomeStatus.QUANTIFIED).model_dump()
    values[field_name] = "" if field_name == "risk_rationale" else ()

    with pytest.raises(ValidationError):
        RiskOrTrendAssessment.model_validate(values)


def test_risk_assessment_rejects_duplicate_codes_and_refs() -> None:
    values = assessment_for(OutcomeStatus.INDETERMINATE).model_dump()
    values["reason_codes"] = (
        IndeterminateReason.INSUFFICIENT_EVIDENCE,
        IndeterminateReason.INSUFFICIENT_EVIDENCE,
    )
    with pytest.raises(ValidationError, match="reason_codes"):
        RiskOrTrendAssessment.model_validate(values)

    values = assessment_for(OutcomeStatus.QUANTIFIED).model_dump()
    values["supporting_refs"] = ("evidence-empty", "evidence-empty")
    with pytest.raises(ValidationError, match="supporting_refs"):
        RiskOrTrendAssessment.model_validate(values)


def test_window_assessment_refs_must_reuse_window_evidence_refs() -> None:
    assessment = RiskOrTrendAssessment(
        summary="Risk is low",
        trend=TrendDirection.STABLE,
        risk_level=RiskLevel.LOW,
        risk_rationale="A different reference was cited.",
        supporting_refs=("evidence-outside-window",),
    )

    with pytest.raises(ValidationError, match="included in evidence_refs"):
        make_point(
            local_day(),
            outcome_status=OutcomeStatus.QUANTIFIED,
            assessment=assessment,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("unit_id", None),
        ("source", None),
        ("confidence_rationale", ""),
        ("evidence_refs", ()),
    ],
)
def test_quantified_requires_unit_source_rationale_and_evidence(
    field_name: str,
    invalid_value: object,
) -> None:
    values = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
    ).model_dump()
    values[field_name] = invalid_value

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("unit_id", "degC"),
        ("source", None),
        ("aggregation_method", AggregationMethod.MEAN),
    ],
)
def test_directional_rejects_illegal_unit_source_or_aggregation(
    field_name: str,
    invalid_value: object,
) -> None:
    values = make_point(
        local_day(),
        outcome_status=OutcomeStatus.DIRECTIONAL,
    ).model_dump()
    values[field_name] = invalid_value

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("unit_id", "degC"),
        ("source", ResultSource.INFERRED),
        ("aggregation_method", AggregationMethod.MEAN),
    ],
)
def test_indeterminate_requires_null_unit_source_and_aggregation(
    field_name: str,
    invalid_value: object,
) -> None:
    values = make_point(local_day()).model_dump()
    values[field_name] = invalid_value

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


def test_fixed_and_agent_selected_indeterminate_confidence_are_distinct() -> None:
    fixed = make_point(local_day())
    assert fixed.source is None
    assert fixed.confidence == 0

    invalid_fixed = fixed.model_dump()
    invalid_fixed["confidence"] = 0.1
    with pytest.raises(ValidationError, match="requires confidence 0"):
        TimeWindowResult.model_validate(invalid_fixed)

    agent_selected = make_point(
        local_day(),
        confidence=0.2,
        assessment=RiskOrTrendAssessment(
            summary="Conflicting supported observations remain inconclusive.",
            trend=TrendDirection.UNCERTAIN,
            risk_level=RiskLevel.INDETERMINATE,
            risk_rationale="The cited observations conflict.",
            reason_codes=(IndeterminateReason.INVALID_OBSERVATIONS,),
            supporting_refs=("evidence-empty",),
        ),
    )
    assert agent_selected.confidence == 0.2


def test_observed_is_one_to_one_and_inferred_may_be_aggregated() -> None:
    observed = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
        source=ResultSource.OBSERVED,
    )
    assert observed.aggregation_method is None

    invalid_observed = observed.model_dump()
    invalid_observed["aggregation_method"] = AggregationMethod.MEAN
    with pytest.raises(ValidationError, match="observed"):
        TimeWindowResult.model_validate(invalid_observed)

    inferred = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
        source=ResultSource.INFERRED,
    )
    assert inferred.aggregation_method is AggregationMethod.MEAN


def test_inferred_directional_shape_cannot_publish_a_quantified_value() -> None:
    directional = make_point(
        local_day(),
        outcome_status=OutcomeStatus.DIRECTIONAL,
        source=ResultSource.INFERRED,
    )

    assert directional.representative_value is None
    assert directional.unit_id is None
    assert directional.aggregation_method is None


def test_indeterminate_null_source_is_valid_in_prediction_segment() -> None:
    requested = local_day()
    result = make_result(
        time_range=requested,
        mode=InferenceMode.PREDICTION,
        time_segments=(
            TimeSegment(
                start=requested.start,
                end=requested.end,
                mode=InferenceMode.PREDICTION,
            ),
        ),
    )

    assert all(point.source is None for point in result.series[0].points)


@pytest.mark.parametrize(
    "field_path",
    (
        "representative_value",
        "window_confidence",
        "series_confidence",
        "overall_confidence",
    ),
)
def test_result_numeric_fields_reject_booleans(field_path: str) -> None:
    requested = local_day()
    windows = build_time_windows(
        requested,
        TimeResolution.TWELVE_HOURS,
        ANALYSIS_TIMEZONE,
    )
    points = tuple(
        make_point(window, outcome_status=OutcomeStatus.QUANTIFIED)
        for window in windows
    )
    series = ParameterTimeSeries(
        parameter=temperature_parameter(),
        time_resolution_source=TimeResolutionSource.DEFAULT,
        points=points,
        confidence=0.5,
        confidence_rationale="The cited cells support the series judgment.",
        contributing_cell_refs=tuple(point.cell_id for point in points),
    )
    values = make_result(time_range=requested, series=(series,)).model_dump()

    if field_path == "representative_value":
        values["series"][0]["points"][0]["representative_value"] = True
    elif field_path == "window_confidence":
        values["series"][0]["points"][0]["confidence"] = True
    elif field_path == "series_confidence":
        values["series"][0]["confidence"] = True
    else:
        values["overall_confidence"] = True

    with pytest.raises(ValidationError):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
@pytest.mark.parametrize("status", list(OutcomeStatus))
def test_confidence_boundaries_are_valid_for_every_outcome(
    confidence: float,
    status: OutcomeStatus,
) -> None:
    assessment = None
    if status is OutcomeStatus.INDETERMINATE:
        assessment = RiskOrTrendAssessment(
            summary="The evidence does not support a water-quality judgment.",
            trend=TrendDirection.UNCERTAIN,
            risk_level=RiskLevel.INDETERMINATE,
            risk_rationale="The cited evidence remains inconclusive.",
            reason_codes=(IndeterminateReason.PARAMETER_UNOBSERVED,),
            supporting_refs=("evidence-empty",),
        )
    point = make_point(
        local_day(),
        outcome_status=status,
        confidence=confidence,
        assessment=assessment,
    )

    assert point.confidence == confidence


@pytest.mark.parametrize("evidence_refs", [(), ("evidence-1", "evidence-1")])
def test_window_result_requires_unique_nonempty_evidence_refs(
    evidence_refs: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        make_point(local_day(), evidence_refs=evidence_refs)


def test_parameter_series_requires_ordered_non_overlapping_points() -> None:
    requested = local_day()
    windows = build_time_windows(
        requested,
        TimeResolution.TWELVE_HOURS,
        ANALYSIS_TIMEZONE,
    )
    first = make_point(windows[0])
    second = make_point(windows[1])
    overlap = make_point(
        TimeRange(
            start=first.end_time - timedelta(hours=1),
            end=second.end_time,
        )
    )

    for points in ((), (second, first), (first, overlap)):
        with pytest.raises(ValidationError):
            ParameterTimeSeries(
                parameter=temperature_parameter(),
                time_resolution_source=TimeResolutionSource.DEFAULT,
                points=points,
                confidence=0,
                confidence_rationale="No supported judgment is available.",
                contributing_cell_refs=tuple(point.cell_id for point in points),
            )


@pytest.mark.parametrize("variant", ["missing", "extra", "wrong_boundary"])
def test_result_rejects_points_that_do_not_exactly_match_window_plan(
    variant: str,
) -> None:
    requested = local_day()
    valid_series = make_series(requested)
    points = list(valid_series.points)
    if variant == "missing":
        points.pop()
    elif variant == "extra":
        points.append(
            make_point(
                TimeRange(
                    start=requested.end,
                    end=requested.end + timedelta(hours=12),
                )
            )
        )
    else:
        first = points[0]
        points[0] = make_point(
            TimeRange(
                start=first.start_time,
                end=first.end_time - timedelta(hours=1),
            )
        )
    invalid_series = ParameterTimeSeries(
        parameter=valid_series.parameter,
        time_resolution=valid_series.time_resolution,
        time_resolution_source=valid_series.time_resolution_source,
        points=tuple(points),
        confidence=valid_series.confidence,
        confidence_rationale=valid_series.confidence_rationale,
        contributing_cell_refs=tuple(point.cell_id for point in points),
    )

    with pytest.raises(ValidationError, match="planned windows"):
        make_result(time_range=requested, series=(invalid_series,))


def test_result_accepts_dst_window_plan_without_elapsed_duration_rule() -> None:
    requested = local_day(month=3, day=8)
    series = make_series(
        requested,
        resolution=TimeResolution.SIX_HOURS,
    )

    result = make_result(
        time_range=requested,
        resolution=TimeResolution.SIX_HOURS,
        series=(series,),
    )

    assert result.series[0].points[0].end_time - result.series[0].points[
        0
    ].start_time == timedelta(hours=5)
    assert_json_round_trip(result)


@pytest.mark.parametrize(
    ("resolution", "expected_windows"),
    [
        (TimeResolution.SIX_HOURS, 4),
        (TimeResolution.TWELVE_HOURS, 2),
        (TimeResolution.TWENTY_FOUR_HOURS, 1),
    ],
)
def test_result_exactly_covers_each_supported_resolution(
    resolution: TimeResolution,
    expected_windows: int,
) -> None:
    requested = local_day()
    series = make_series(requested, resolution=resolution)
    result = make_result(
        time_range=requested,
        resolution=resolution,
        series=(series,),
    )

    assert len(result.series[0].points) == expected_windows
    assert len(result.window_assessments) == expected_windows


@pytest.mark.parametrize(
    ("month", "day", "resolution", "elapsed_hours"),
    [
        (3, 8, TimeResolution.SIX_HOURS, 5),
        (11, 1, TimeResolution.SIX_HOURS, 7),
        (3, 8, TimeResolution.TWENTY_FOUR_HOURS, 23),
        (11, 1, TimeResolution.TWENTY_FOUR_HOURS, 25),
    ],
)
def test_dst_windows_use_local_boundaries_not_fixed_elapsed_hours(
    month: int,
    day: int,
    resolution: TimeResolution,
    elapsed_hours: int,
) -> None:
    requested = local_day(month=month, day=day)
    series = make_series(requested, resolution=resolution)
    result = make_result(
        time_range=requested,
        resolution=resolution,
        series=(series,),
    )

    assert any(
        point.end_time - point.start_time == timedelta(hours=elapsed_hours)
        for point in result.series[0].points
    )


def test_series_rejects_duplicate_cells_and_mismatched_result_refs() -> None:
    series = make_series(local_day())
    values = series.model_dump()
    values["points"][1]["cell_id"] = values["points"][0]["cell_id"]
    values["contributing_cell_refs"] = tuple(
        point["cell_id"] for point in values["points"]
    )
    with pytest.raises(ValidationError, match="cell IDs"):
        ParameterTimeSeries.model_validate(values)

    values = series.model_dump()
    values["contributing_cell_refs"] = (series.points[0].cell_id,)
    with pytest.raises(ValidationError, match="exactly match"):
        ParameterTimeSeries.model_validate(values)


@pytest.mark.parametrize("variant", ["missing", "extra", "reordered"])
def test_result_requires_one_ordered_composite_per_planned_window(
    variant: str,
) -> None:
    result = make_result()
    values = result.model_dump()
    assessments = list(values["window_assessments"])
    if variant == "missing":
        assessments.pop()
    elif variant == "extra":
        assessments.append(assessments[-1])
    else:
        assessments.reverse()
    values["window_assessments"] = assessments

    with pytest.raises(ValidationError, match="window"):
        InferenceResult.model_validate(values)


def test_requested_parameter_order_must_match_series_order() -> None:
    requested = local_day()
    first = make_series(requested)
    second = make_series(
        requested,
        parameter=temperature_parameter(code_system="epa"),
    )
    result = make_result(series=(first, second))
    values = result.model_dump()
    values["requested_parameters"] = tuple(
        reversed(values["requested_parameters"])
    )

    with pytest.raises(ValidationError, match="parameter order"):
        InferenceResult.model_validate(values)


def test_result_rejects_duplicate_parameter_keys() -> None:
    result = make_result()
    values = result.model_dump()
    values["requested_parameters"] = (
        result.requested_parameters[0],
        result.requested_parameters[0],
    )

    with pytest.raises(ValidationError, match="requested_parameters"):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize(
    "variant",
    ["empty", "incomplete", "gap", "overlap", "out_of_order"],
)
def test_result_requires_segments_to_exactly_cover_request(
    variant: str,
) -> None:
    requested = local_day()
    midpoint = requested.start + timedelta(hours=12)
    if variant == "empty":
        segments: tuple[TimeSegment, ...] = ()
    elif variant == "incomplete":
        segments = (
            TimeSegment(
                start=requested.start,
                end=midpoint,
                mode=InferenceMode.GAP_FILLING,
            ),
        )
    elif variant == "gap":
        segments = (
            TimeSegment(
                start=requested.start,
                end=midpoint - timedelta(hours=1),
                mode=InferenceMode.GAP_FILLING,
            ),
            TimeSegment(
                start=midpoint,
                end=requested.end,
                mode=InferenceMode.GAP_FILLING,
            ),
        )
    elif variant == "overlap":
        segments = (
            TimeSegment(
                start=requested.start,
                end=midpoint + timedelta(hours=1),
                mode=InferenceMode.GAP_FILLING,
            ),
            TimeSegment(
                start=midpoint,
                end=requested.end,
                mode=InferenceMode.GAP_FILLING,
            ),
        )
    else:
        segments = (
            TimeSegment(
                start=midpoint,
                end=requested.end,
                mode=InferenceMode.GAP_FILLING,
            ),
            TimeSegment(
                start=requested.start,
                end=midpoint,
                mode=InferenceMode.GAP_FILLING,
            ),
        )

    with pytest.raises(ValidationError):
        make_result(time_range=requested, time_segments=segments)


def test_result_requires_at_least_one_series() -> None:
    values = make_result().model_dump()
    values["series"] = ()

    with pytest.raises(ValidationError, match="series must not be empty"):
        InferenceResult.model_validate(values)


def test_mixed_result_requires_both_segment_modes_and_matching_sources() -> None:
    requested = local_day()
    midpoint = build_time_windows(
        requested,
        TimeResolution.TWELVE_HOURS,
        ANALYSIS_TIMEZONE,
    )[0].end
    segments = (
        TimeSegment(
            start=requested.start,
            end=midpoint,
            mode=InferenceMode.GAP_FILLING,
        ),
        TimeSegment(
            start=midpoint,
            end=requested.end,
            mode=InferenceMode.PREDICTION,
        ),
    )
    series = make_series(
        requested,
        point_sources=(ResultSource.INFERRED, ResultSource.PREDICTED),
    )

    result = make_result(
        time_range=requested,
        mode=InferenceMode.MIXED,
        time_segments=segments,
        series=(series,),
    )

    assert_json_round_trip(result)

    with pytest.raises(ValidationError, match="requires gap_filling segments followed"):
        make_result(
            time_range=requested,
            mode=InferenceMode.MIXED,
            time_segments=(
                TimeSegment(
                    start=requested.start,
                    end=requested.end,
                    mode=InferenceMode.GAP_FILLING,
                ),
            ),
            series=(make_series(requested),),
        )


@pytest.mark.parametrize(
    "segment_modes",
    (
        (
            InferenceMode.PREDICTION,
            InferenceMode.PREDICTION,
            InferenceMode.GAP_FILLING,
            InferenceMode.GAP_FILLING,
        ),
        (
            InferenceMode.GAP_FILLING,
            InferenceMode.PREDICTION,
            InferenceMode.GAP_FILLING,
            InferenceMode.PREDICTION,
        ),
        (
            InferenceMode.PREDICTION,
            InferenceMode.GAP_FILLING,
            InferenceMode.PREDICTION,
            InferenceMode.PREDICTION,
        ),
    ),
)
def test_mixed_result_rejects_reversed_or_repeated_mode_transitions(
    segment_modes: tuple[InferenceMode, ...],
) -> None:
    requested = local_day()
    windows = build_time_windows(
        requested,
        TimeResolution.SIX_HOURS,
        ANALYSIS_TIMEZONE,
    )
    segments = tuple(
        TimeSegment(start=window.start, end=window.end, mode=mode)
        for window, mode in zip(windows, segment_modes, strict=True)
    )
    point_sources = tuple(
        ResultSource.PREDICTED
        if mode is InferenceMode.PREDICTION
        else ResultSource.INFERRED
        for mode in segment_modes
    )
    series = make_series(
        requested,
        resolution=TimeResolution.SIX_HOURS,
        point_sources=point_sources,
    )

    with pytest.raises(ValidationError, match="requires gap_filling segments followed"):
        make_result(
            time_range=requested,
            mode=InferenceMode.MIXED,
            time_segments=segments,
            resolution=TimeResolution.SIX_HOURS,
            series=(series,),
        )


@pytest.mark.parametrize(
    ("mode", "segment_mode", "point_source", "message"),
    [
        (
            InferenceMode.PREDICTION,
            InferenceMode.PREDICTION,
            ResultSource.OBSERVED,
            "prediction segment",
        ),
        (
            InferenceMode.GAP_FILLING,
            InferenceMode.GAP_FILLING,
            ResultSource.PREDICTED,
            "gap_filling segment",
        ),
    ],
)
def test_result_rejects_source_incompatible_with_segment(
    mode: InferenceMode,
    segment_mode: InferenceMode,
    point_source: ResultSource,
    message: str,
) -> None:
    requested = local_day()
    segment = TimeSegment(
        start=requested.start,
        end=requested.end,
        mode=segment_mode,
    )
    series = make_series(
        requested,
        point_sources=(point_source, point_source),
    )

    with pytest.raises(ValidationError, match=message):
        make_result(
            time_range=requested,
            mode=mode,
            time_segments=(segment,),
            series=(series,),
        )


@pytest.mark.parametrize("source", [ResultSource.OBSERVED, ResultSource.INFERRED])
def test_gap_filling_accepts_observed_and_inferred_sources(
    source: ResultSource,
) -> None:
    requested = local_day()
    series = make_series(requested, point_sources=(source, source))

    assert make_result(time_range=requested, series=(series,)).series == (series,)


@pytest.mark.parametrize("mismatch", ["resolution", "source"])
def test_series_resolution_metadata_must_match_result(mismatch: str) -> None:
    requested = local_day()
    series = make_series(
        requested,
        resolution=(
            TimeResolution.SIX_HOURS
            if mismatch == "resolution"
            else TimeResolution.TWELVE_HOURS
        ),
        resolution_source=(
            TimeResolutionSource.AUTO
            if mismatch == "source"
            else TimeResolutionSource.DEFAULT
        ),
    )

    with pytest.raises(ValidationError, match="must match result"):
        make_result(time_range=requested, series=(series,))


def test_window_must_fit_entirely_within_one_segment() -> None:
    requested = local_day()
    split = requested.start + timedelta(hours=6)
    segments = (
        TimeSegment(
            start=requested.start,
            end=split,
            mode=InferenceMode.GAP_FILLING,
        ),
        TimeSegment(
            start=split,
            end=requested.end,
            mode=InferenceMode.PREDICTION,
        ),
    )

    with pytest.raises(ValidationError, match="fit within one time segment"):
        make_result(
            time_range=requested,
            mode=InferenceMode.MIXED,
            time_segments=segments,
        )


def test_partial_availability_keeps_risk_and_confidence_independent() -> None:
    result = make_partial_result()

    assert all(
        assessment.availability is AssessmentAvailability.PARTIAL
        for assessment in result.window_assessments
    )
    assert result.overall_assessment.availability is AssessmentAvailability.PARTIAL
    assert result.overall_assessment.risk_level is RiskLevel.NONE
    assert result.overall_assessment.confidence == 0.5
    assert result.overall_assessment.assessed_cell_count == 2
    assert result.overall_assessment.total_cell_count == 4


@pytest.mark.parametrize(
    ("target", "field_name", "invalid_value"),
    [
        ("window", "assessed_cell_count", 2),
        ("window", "total_cell_count", 3),
        ("overall", "assessed_cell_count", 4),
        ("overall", "missing_cell_refs", ()),
    ],
)
def test_availability_counts_and_missing_refs_must_agree(
    target: str,
    field_name: str,
    invalid_value: object,
) -> None:
    result = make_partial_result()
    values = result.model_dump()
    assessment = (
        values["window_assessments"][0]
        if target == "window"
        else values["overall_assessment"]
    )
    assessment[field_name] = invalid_value

    with pytest.raises(ValidationError):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize("target", ["window", "overall"])
def test_none_risk_requires_supporting_result_and_evidence_refs(
    target: str,
) -> None:
    result = make_partial_result()
    values = result.model_dump()
    assessment = (
        values["window_assessments"][0]
        if target == "window"
        else values["overall_assessment"]
    )
    assessment["evidence_refs"] = ()

    with pytest.raises(ValidationError, match="evidence"):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize(
    ("target", "field_name"),
    [
        ("window", "risk_rationale"),
        ("window", "confidence_rationale"),
        ("overall", "risk_rationale"),
        ("overall", "confidence_rationale"),
    ],
)
def test_composite_and_overall_require_nonempty_rationales(
    target: str,
    field_name: str,
) -> None:
    values = make_partial_result().model_dump()
    assessment = (
        values["window_assessments"][0]
        if target == "window"
        else values["overall_assessment"]
    )
    assessment[field_name] = ""

    with pytest.raises(ValidationError):
        InferenceResult.model_validate(values)


def test_overall_confidence_fields_must_match_overall_assessment() -> None:
    values = make_partial_result().model_dump()
    values["overall_confidence"] = 0.9
    with pytest.raises(ValidationError, match="overall_confidence"):
        InferenceResult.model_validate(values)

    values = make_partial_result().model_dump()
    values["overall_confidence_rationale"] = "A different rationale."
    with pytest.raises(ValidationError, match="overall_confidence_rationale"):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("window_assessments", 0, "risk_level"), 0.5),
        (("overall_assessment", "risk_level"), 0.5),
        (("model_execution_summary", "planning_calls"), True),
        (("model_execution_summary", "skipped_cells"), -1),
        (("model_execution_summary", "usage", "input_tokens"), True),
    ],
)
def test_assessment_enums_and_execution_numbers_are_strict(
    path: tuple[object, ...],
    invalid_value: object,
) -> None:
    values = make_partial_result().model_dump()
    target: object = values
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = invalid_value  # type: ignore[index]

    with pytest.raises(ValidationError):
        InferenceResult.model_validate(values)


def test_execution_summary_rejects_secret_fields_and_wrong_skipped_count() -> None:
    values = make_partial_result().model_dump()
    values["model_execution_summary"]["api_key"] = "secret"
    with pytest.raises(ValidationError, match="Extra inputs"):
        InferenceResult.model_validate(values)

    values = make_partial_result().model_dump()
    values["model_execution_summary"]["skipped_cells"] = 0
    with pytest.raises(ValidationError, match="skipped_cells"):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize(
    ("owner", "field_path"),
    [
        ("window assessment", ("window_assessments", 0, "contributing_cell_refs")),
        ("overall assessment", ("overall_assessment", "contributing_cell_refs")),
        ("overall assessment", ("overall_assessment", "contributing_window_refs")),
    ],
)
def test_result_rejects_cross_result_or_unknown_result_refs(
    owner: str,
    field_path: tuple[object, ...],
) -> None:
    values = make_partial_result().model_dump()
    target: object = values
    for key in field_path[:-1]:
        target = target[key]  # type: ignore[index]
    refs = list(target[field_path[-1]])  # type: ignore[index]
    refs[0] = "cross-result:unknown"
    target[field_path[-1]] = tuple(refs)  # type: ignore[index]

    with pytest.raises(ValidationError, match=owner):
        InferenceResult.model_validate(values)


@pytest.mark.parametrize("owner", ["window", "attribution", "analysis step"])
def test_result_rejects_references_outside_evidence_chain(owner: str) -> None:
    requested = local_day()
    series = None
    attribution = None
    detail = None
    if owner == "window":
        series = (
            make_series(
                requested,
                evidence_refs=(("unknown",), ("evidence-empty",)),
            ),
        )
    elif owner == "attribution":
        attribution = AttributionSummary(
            summary="Possible upstream factor",
            primary_factors=(
                AttributionFactor(
                    factor="Upstream discharge",
                    direction=AttributionDirection.INCREASING,
                    magnitude=AttributionMagnitude.MEDIUM,
                    basis=AttributionBasis.CORRELATED,
                    explanation="Discharge rose before the target window",
                    evidence_refs=("unknown",),
                ),
            ),
        )
    else:
        detail = AnalysisDetail(
            summary="Analysis complete",
            steps=(
                AnalysisStep(
                    type="evidence_review",
                    title="Review evidence",
                    content={"status": "complete"},
                    evidence_refs=("unknown",),
                ),
            ),
        )

    with pytest.raises(ValidationError, match=f"{owner} references evidence"):
        make_result(
            time_range=requested,
            series=series,
            attribution_summary=attribution,
            analysis_detail=detail,
        )


@pytest.mark.parametrize(
    ("owner", "target", "field_name"),
    [
        ("window composite", "window", "evidence_refs"),
        ("window blocker", "window", "blocker_refs"),
        ("overall assessment", "overall", "evidence_refs"),
        ("overall blocker", "overall", "blocker_refs"),
    ],
)
def test_result_closes_composite_overall_and_blocker_evidence_refs(
    owner: str,
    target: str,
    field_name: str,
) -> None:
    values = make_result().model_dump()
    assessment = (
        values["window_assessments"][0]
        if target == "window"
        else values["overall_assessment"]
    )
    assessment[field_name] = ("unknown",)

    with pytest.raises(ValidationError, match=f"{owner} references evidence"):
        InferenceResult.model_validate(values)


def test_attribution_and_analysis_timeline_round_trip() -> None:
    attribution = AttributionSummary(
        summary="Upstream flow may be contributing",
        primary_factors=(
            AttributionFactor(
                factor="Upstream discharge",
                direction=AttributionDirection.INCREASING,
                magnitude=AttributionMagnitude.MEDIUM,
                basis=AttributionBasis.HYPOTHESIZED,
                explanation="Timing is consistent but causality is not established",
                evidence_refs=("evidence-empty",),
            ),
        ),
    )
    detail = AnalysisDetail(
        summary="Reviewed station coverage and upstream context",
        steps=(
            AnalysisStep(
                type="station_search",
                title="Find observations",
                content={"collection_status": "empty", "station_count": 0},
                evidence_refs=("evidence-empty",),
                transcript_item_refs=("transcript-1",),
            ),
        ),
        transcript_item_refs=("transcript-1",),
    )

    result = make_result(
        attribution_summary=attribution,
        analysis_detail=detail,
    )

    assert result.attribution_summary.primary_factors[0].basis is (
        AttributionBasis.HYPOTHESIZED
    )
    assert set(result.analysis_detail.steps[0].model_dump()) == {
        "type",
        "title",
        "content",
        "evidence_refs",
        "result_cell_refs",
        "transcript_item_refs",
    }
    assert_json_round_trip(result)


def test_analysis_and_attribution_result_refs_must_resolve_to_result_cells() -> None:
    baseline = make_result()
    payload = baseline.model_dump(mode="python")
    payload["analysis_detail"] = AnalysisDetail(
        summary="Invalid result reference fixture",
        steps=(
            AnalysisStep(
                type="review",
                title="Review result",
                content="The reference must resolve within this result.",
                result_cell_refs=("cell:unknown",),
            ),
        ),
    )

    with pytest.raises(ValidationError, match="unknown result cells"):
        InferenceResult.model_validate(payload)


def test_attribution_allows_no_factors_but_each_factor_requires_evidence() -> None:
    summary = AttributionSummary(summary="No evidence supports specific attribution")

    assert summary.primary_factors == ()
    assert_json_round_trip(summary)

    with pytest.raises(ValidationError, match="must not be empty"):
        AttributionFactor(
            factor="Upstream discharge",
            direction=AttributionDirection.UNKNOWN,
            magnitude=AttributionMagnitude.UNKNOWN,
            basis=AttributionBasis.HYPOTHESIZED,
            explanation="No supporting evidence",
            evidence_refs=(),
        )


def make_unavailable_fragment(
    evidence_id: str,
    status: CollectionStatus,
    time_range: TimeRange,
) -> EvidenceFragment:
    return EvidenceFragment(
        evidence_id=evidence_id,
        source="station_observation",
        collection_status=status,
        target_comid="001234",
        location=EvidenceLocation(
            point=None,
            comid="001234",
            station_id=None,
            distance_to_target_km=None,
        ),
        time_range=time_range,
        parameter_refs=(ParameterKey(code_system="usgs", code="00010"),),
        data_type="water_temperature",
        applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
        series_identity="sha256:temperature-series",
        quality_class=None,
        quality_flags=(),
        coverage=EvidenceCoverage(
            status=CoverageStatus.INSUFFICIENT,
            observed_count=0,
            expected_count=None,
            ratio=None,
            covered_duration_seconds=0,
            window_duration_seconds=int(
                (time_range.end - time_range.start).total_seconds()
            ),
            largest_gap_seconds=None,
        ),
        payload=CollectionFactPayload(
            status=status,
            reason=f"collection_{status.value}",
            affected_window_ids=("w000", "w001"),
            record_refs=(),
        ),
        source_record_refs=(),
        derivation=None,
        provenance=Provenance(
            operation_id=f"operation-{status.value}",
            registry_digest="sha256:registry",
            request_identity="sha256:request",
            page_identity=None,
            chunk_identity=None,
            provider_series_id="temperature-series",
            provider_record_id=None,
            source_timezone=None,
            source_utc_offset=None,
        ),
        result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
    )


def test_complete_no_evidence_result_keeps_every_window_indeterminate() -> None:
    requested = local_day()
    empty = make_unavailable_fragment(
        "evidence-empty",
        CollectionStatus.EMPTY,
        requested,
    )
    unavailable = make_unavailable_fragment(
        "evidence-unavailable",
        CollectionStatus.UNAVAILABLE,
        requested,
    )
    series = make_series(
        requested,
        evidence_refs=(("evidence-empty",), ("evidence-unavailable",)),
    )
    result = make_result(
        time_range=requested,
        series=(series,),
        evidence_chain=make_chain("evidence-empty", "evidence-unavailable"),
        overall_confidence=0,
    )

    status_by_id = {
        fragment.evidence_id: fragment.collection_status
        for fragment in (empty, unavailable)
    }
    assert all(
        point.outcome_status is OutcomeStatus.INDETERMINATE
        for point in result.series[0].points
    )
    assert {
        status_by_id[reference]
        for point in result.series[0].points
        for reference in point.evidence_refs
    } == {CollectionStatus.EMPTY, CollectionStatus.UNAVAILABLE}
    assert_json_round_trip(result)
