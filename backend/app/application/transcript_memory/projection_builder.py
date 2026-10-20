from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from app.application.transcript_memory.case_policy import (
    canonical_intent_tokens,
    meteorological_season,
    meteorological_season_year,
)
from app.application.transcript_memory.source_closure import validate_source_closure
from app.application.transcript_memory.source_models import RAGCaseSource
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.serialization import canonical_model_bytes
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.memory_rag.canonical_case_query import CanonicalCaseQueryV1
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.historical_case_identity import derive_historical_case_id
from app.domain.memory_rag.historical_result_cell_projection import (
    HistoricalResultCellProjection,
)
from app.domain.memory_rag.historical_result_projection import HistoricalResultProjection
from app.domain.memory_rag.meteorological_season import MeteorologicalSeason
from app.domain.memory_rag.prepared_rag_case_projection import (
    PreparedRAGCaseProjectionV1,
)
from app.domain.memory_rag.rag_case_window import RAGCaseWindowV1
from app.domain.memory_rag.rag_fragment_projection import RAGFragmentProjectionV1
from app.domain.memory_rag.rag_quality_tier import RAGQualityTier
from app.domain.time.timezone import get_iana_timezone

class RAGProjectionBuildError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedRAGProjectionBundle:
    case: PreparedRAGCaseProjectionV1
    fragments: tuple[RAGFragmentProjectionV1, ...]


def build_prepared_projection(
    source: RAGCaseSource,
    *,
    generation_id: str,
    profile_id: str,
) -> PreparedRAGProjectionBundle:
    _require_uuid(generation_id, "generation_id")
    validated = validate_source_closure(source)
    context = source.analysis_context.body
    result = source.result.body
    windows, window_ids = _case_windows(result, context)
    result_projection = HistoricalResultProjection(
        mode=result.mode,
        source_completed_at=source.session.updated_at,
        overall_risk=result.overall_assessment.risk_level,
        overall_confidence=result.overall_confidence,
        availability=result.overall_assessment.availability,
        cells=_result_cells(result, window_ids),
    )
    canonical = CanonicalCaseQueryV1(
        mode=context.mode,
        requested_parameter_keys=tuple(item.key for item in context.params_used),
        supporting_parameter_keys=tuple(
            item.key for item in context.supporting_params
        ),
        window_descriptors=tuple(window.canonical_descriptor() for window in windows),
        normalized_intent_tokens=canonical_intent_tokens(
            context.mode,
            tuple(item.key for item in context.supporting_params),
        ),
    )
    case_ref = HistoricalCaseRef(
        generation_id=generation_id,
        case_id=str(
            derive_historical_case_id(
                generation_id,
                source.result.item.run_id,
                source.result.item.session_id,
                source.result.item.item_id,
            )
        ),
        run_id=str(source.result.item.run_id),
        session_id=str(source.result.item.session_id),
        target_comid=result.target_comid,
        analysis_context_item_id=str(source.analysis_context.item.item_id),
        result_item_id=str(source.result.item.item_id),
    )
    case = PreparedRAGCaseProjectionV1(
        case_ref=case_ref,
        profile_id=profile_id,
        mode=result.mode,
        parameter_keys=result.requested_parameters,
        window_ids=window_ids,
        windows=windows,
        source_completed_at=source.session.updated_at,
        canonical_query=canonical.canonical_text,
        canonical_query_digest=canonical.canonical_digest,
        result_projection=result_projection,
    )
    fragments = tuple(
        sorted(
            (
                projection
                for item in validated.eligible_fragments
                for projection in _fragment_projections(
                    case_ref,
                    result,
                    item,
                    window_ids,
                )
            ),
            key=lambda item: (
                item.source_fragment_item_id,
                item.parameter.code_system,
                item.parameter.code,
                item.window_id,
            ),
        )
    )
    return PreparedRAGProjectionBundle(case=case, fragments=fragments)


def _case_windows(result, context) -> tuple[tuple[RAGCaseWindowV1, ...], tuple[str, ...]]:
    if not result.series:
        raise RAGProjectionBuildError("source_result_has_no_series")
    point_count = len(result.series[0].points)
    if any(len(series.points) != point_count for series in result.series):
        raise RAGProjectionBuildError("source_result_window_count_mismatch")
    window_ids: list[str] = []
    windows: list[RAGCaseWindowV1] = []
    zone = get_iana_timezone(context.analysis_timezone)
    for index in range(point_count):
        ids = {
            _window_id(series.parameter.key, series.points[index].cell_id)
            for series in result.series
        }
        if len(ids) != 1:
            raise RAGProjectionBuildError("source_result_window_identity_mismatch")
        window_id = ids.pop()
        points = tuple(series.points[index] for series in result.series)
        starts = {point.start_time for point in points}
        ends = {point.end_time for point in points}
        if len(starts) != 1 or len(ends) != 1:
            raise RAGProjectionBuildError("source_result_window_bounds_mismatch")
        start = starts.pop()
        end = ends.pop()
        segment = next(
            (
                item
                for item in context.time_segments
                if item.time_range.start <= start and end <= item.time_range.end
            ),
            None,
        )
        if segment is None:
            raise RAGProjectionBuildError("source_result_window_segment_mismatch")
        touched = (
            start.astimezone(zone),
            (end - timedelta(microseconds=1)).astimezone(zone),
        )
        months = tuple(sorted({value.month for value in touched}))
        seasons = tuple(
            sorted(
                {meteorological_season(value.month) for value in touched},
                key=lambda item: tuple(MeteorologicalSeason).index(item),
            )
        )
        season_years = {
            meteorological_season_year(value.year, value.month) for value in touched
        }
        if len(season_years) != 1:
            raise RAGProjectionBuildError("source_result_season_year_mismatch")
        duration = (end - start).total_seconds()
        if not duration.is_integer():
            raise RAGProjectionBuildError("source_result_window_duration_invalid")
        windows.append(
            RAGCaseWindowV1(
                window_id=window_id,
                time_range={"start": start, "end": end},
                window_kind=segment.mode,
                local_months=months,
                season_buckets=seasons,
                season_year=season_years.pop(),
                duration_seconds=int(duration),
                resolution=context.time_resolution,
                parameter_keys=result.requested_parameters,
            )
        )
        window_ids.append(window_id)
    return tuple(windows), tuple(window_ids)


def _result_cells(result, window_ids: tuple[str, ...]) -> tuple[HistoricalResultCellProjection, ...]:
    return tuple(
        HistoricalResultCellProjection(
            parameter=series.parameter.key,
            window_id=window_ids[index],
            time_range={"start": point.start_time, "end": point.end_time},
            outcome_status=point.outcome_status,
            representative_value=point.representative_value,
            value_lower=point.value_lower,
            value_upper=point.value_upper,
            unit_id=point.unit_id,
            trend=point.assessment.trend,
            risk_level=point.assessment.risk_level,
            confidence=point.confidence,
            source=point.source,
            quality_flags=point.quality_flags,
            inference_method=point.inference_method,
            inference_basis=point.inference_basis,
            measured_anchors=point.measured_anchors,
        )
        for series in result.series
        for index, point in enumerate(series.points)
    )


def _fragment_projections(
    case_ref,
    result,
    item,
    window_ids,
) -> tuple[RAGFragmentProjectionV1, ...]:
    fragment = item.body
    matches = tuple(
        (series.parameter.key, window_ids[index])
        for series in result.series
        for index, point in enumerate(series.points)
        if fragment.evidence_id in point.evidence_refs
    )
    if not matches:
        raise RAGProjectionBuildError("eligible_fragment_has_no_result_cell")
    provenance_identity = f"sha256:{sha256(canonical_model_bytes(fragment.provenance)).hexdigest()}"
    return tuple(
        RAGFragmentProjectionV1(
            case_ref=case_ref,
            source_fragment_item_id=str(item.item.item_id),
            evidence_id=fragment.evidence_id,
            parameter=parameter,
            window_id=window_id,
            valid_time_range=fragment.time_range,
            quality_tier=RAGQualityTier(fragment.quality_class.value),
            quality_flags=fragment.quality_flags,
            coverage=fragment.coverage,
            applicability=fragment.applicability.value,
            result_eligibility=fragment.result_eligibility,
            provenance_identity=provenance_identity,
        )
        for parameter, window_id in matches
    )


def _window_id(parameter: ParameterKey, cell_id: str) -> str:
    prefix = f"cell:{parameter.code_system}:{parameter.code}:"
    if not cell_id.startswith(prefix) or cell_id == prefix:
        raise RAGProjectionBuildError("source_result_cell_identity_invalid")
    return cell_id.removeprefix(prefix)


def _require_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise RAGProjectionBuildError(f"{field_name}_invalid") from error
