from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain import (
    AggregationMethod,
    AnalysisDetail,
    AnalysisStep,
    AttributionBasis,
    AttributionDirection,
    AttributionFactor,
    AttributionMagnitude,
    AttributionSummary,
    CollectionStatus,
    EvidenceBundle,
    EvidenceChain,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceReference,
    IndeterminateReason,
    InferenceMode,
    InferenceResult,
    OutcomeStatus,
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


def assessment_for(status: OutcomeStatus) -> RiskOrTrendAssessment | None:
    if status is OutcomeStatus.DIRECTIONAL:
        return RiskOrTrendAssessment(
            summary="Temperature is rising",
            trend=TrendDirection.RISING,
            risk_level=None,
        )
    if status is OutcomeStatus.INDETERMINATE:
        return RiskOrTrendAssessment(
            summary="No usable station observation",
            trend=None,
            risk_level=RiskLevel.INDETERMINATE,
            reason_codes=(IndeterminateReason.PARAMETER_UNOBSERVED,),
        )
    return None


def make_point(
    window: TimeRange,
    *,
    outcome_status: OutcomeStatus = OutcomeStatus.INDETERMINATE,
    source: ResultSource = ResultSource.INFERRED,
    evidence_refs: tuple[str, ...] = ("evidence-empty",),
    confidence: float = 0.5,
    assessment: RiskOrTrendAssessment | None = None,
) -> TimeWindowResult:
    return TimeWindowResult(
        start_time=window.start,
        end_time=window.end,
        outcome_status=outcome_status,
        representative_value=(
            7.4 if outcome_status is OutcomeStatus.QUANTIFIED else None
        ),
        assessment=assessment if assessment is not None else assessment_for(outcome_status),
        source=source,
        confidence=confidence,
        aggregation_method=(
            AggregationMethod.MEAN
            if outcome_status is OutcomeStatus.QUANTIFIED
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
    evidence_refs: tuple[tuple[str, ...], ...] | None = None,
    parameter: WaterQualityParameter | None = None,
) -> ParameterTimeSeries:
    windows = build_time_windows(time_range, resolution, ANALYSIS_TIMEZONE)
    sources = point_sources or (ResultSource.INFERRED,) * len(windows)
    refs = evidence_refs or (("evidence-empty",),) * len(windows)
    return ParameterTimeSeries(
        parameter=parameter or temperature_parameter(),
        time_resolution=resolution,
        time_resolution_source=resolution_source,
        points=tuple(
            make_point(window, source=source, evidence_refs=point_refs)
            for window, source, point_refs in zip(windows, sources, refs, strict=True)
        ),
        confidence=0.5,
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
    overall_confidence: float = 0.5,
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
    return InferenceResult(
        result_id="result-1",
        target_comid="001234",
        mode=mode,
        time_range=requested,
        time_segments=segments,
        analysis_timezone=ANALYSIS_TIMEZONE,
        time_resolution=resolution,
        time_resolution_source=resolution_source,
        series=result_series,
        overall_confidence=overall_confidence,
        attribution_summary=attribution_summary
        or AttributionSummary(summary="Insufficient attribution evidence"),
        analysis_detail=analysis_detail
        or AnalysisDetail(summary="Analysis complete", steps=()),
        evidence_chain=evidence_chain or make_chain("evidence-empty"),
        generated_at=datetime(2026, 2, 2, 12, tzinfo=timezone.utc),
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
        "low",
        "medium",
        "high",
        "indeterminate",
    ]
    assert [reason.value for reason in IndeterminateReason] == [
        "no_station_path",
        "parameter_unobserved",
        "invalid_observations",
        "source_unavailable",
        "no_applicable_evidence",
    ]


@pytest.mark.parametrize("with_assessment", [False, True])
def test_quantified_accepts_value_with_optional_assessment(
    with_assessment: bool,
) -> None:
    assessment = None
    if with_assessment:
        assessment = RiskOrTrendAssessment(
            summary="Low risk and stable",
            trend=TrendDirection.STABLE,
            risk_level=RiskLevel.LOW,
        )

    point = make_point(
        local_day(),
        outcome_status=OutcomeStatus.QUANTIFIED,
        assessment=assessment,
    )

    assert point.representative_value == 7.4
    assert_json_round_trip(point)


def test_directional_accepts_a_definite_trend_or_risk() -> None:
    point = make_point(
        local_day(),
        outcome_status=OutcomeStatus.DIRECTIONAL,
        assessment=RiskOrTrendAssessment(
            summary="Risk remains high",
            trend=TrendDirection.UNCERTAIN,
            risk_level=RiskLevel.HIGH,
        ),
    )

    assert point.representative_value is None
    assert_json_round_trip(point)


@pytest.mark.parametrize(
    ("representative_value", "assessment"),
    [
        (7.4, assessment_for(OutcomeStatus.DIRECTIONAL)),
        (None, None),
        (
            None,
            RiskOrTrendAssessment(
                summary="Still uncertain",
                trend=TrendDirection.UNCERTAIN,
                risk_level=RiskLevel.INDETERMINATE,
            ),
        ),
        (
            None,
            RiskOrTrendAssessment(
                summary="Invalid reason placement",
                trend=TrendDirection.RISING,
                risk_level=None,
                reason_codes=(IndeterminateReason.NO_APPLICABLE_EVIDENCE,),
            ),
        ),
    ],
)
def test_directional_rejects_illegal_field_combinations(
    representative_value: float | None,
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
        (7.4, assessment_for(OutcomeStatus.INDETERMINATE)),
        (None, None),
        (
            None,
            RiskOrTrendAssessment(
                summary="No structured reason",
                trend=None,
                risk_level=RiskLevel.INDETERMINATE,
            ),
        ),
    ],
)
def test_indeterminate_rejects_illegal_field_combinations(
    representative_value: float | None,
    assessment: RiskOrTrendAssessment | None,
) -> None:
    values = make_point(local_day()).model_dump()
    values["representative_value"] = representative_value
    values["assessment"] = assessment

    with pytest.raises(ValidationError):
        TimeWindowResult.model_validate(values)


def test_quantified_requires_a_finite_value() -> None:
    valid = make_point(local_day(), outcome_status=OutcomeStatus.QUANTIFIED)

    for value in (None, float("nan"), float("inf")):
        values = valid.model_dump()
        values["representative_value"] = value
        with pytest.raises(ValidationError):
            TimeWindowResult.model_validate(values)


@pytest.mark.parametrize("confidence", [0.0, 1.0])
@pytest.mark.parametrize("status", list(OutcomeStatus))
def test_confidence_boundaries_are_valid_for_every_outcome(
    confidence: float,
    status: OutcomeStatus,
) -> None:
    point = make_point(local_day(), outcome_status=status, confidence=confidence)

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
                confidence=0.5,
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


def test_result_rejects_duplicate_parameter_keys() -> None:
    requested = local_day()
    first = make_series(requested)
    duplicate_key = make_series(
        requested,
        parameter=WaterQualityParameter(
            key=first.parameter.key,
            name="Alternate label",
            canonical_unit="degC",
            category=None,
        ),
    )

    with pytest.raises(ValidationError, match="parameter keys"):
        make_result(series=(first, duplicate_key))


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
    with pytest.raises(ValidationError, match="series must not be empty"):
        make_result(series=())


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

    with pytest.raises(ValidationError, match="requires gap_filling and prediction"):
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
        "transcript_item_refs",
    }
    assert_json_round_trip(result)


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
        value_or_summary={"reason": status.value},
        confidence_hint=None,
        provenance=Provenance(
            provider="usgs",
            retrieved_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
            source_uri=None,
            source_record_ids=(),
            source_timezone=None,
            source_utc_offset=None,
        ),
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
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        agent_comid="001234",
        time_range=requested,
        analysis_timezone=ANALYSIS_TIMEZONE,
        time_resolution_source=TimeResolutionSource.DEFAULT,
        requested_parameters=(ParameterKey(code_system="usgs", code="00010"),),
        fragments=(empty, unavailable),
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
        for fragment in bundle.fragments
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
