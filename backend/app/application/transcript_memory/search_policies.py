from __future__ import annotations

from datetime import datetime
from math import isfinite

from app.domain import (
    AuthorizedRAGComid,
    AuthorizedRAGSearchQuery,
    HistoricalMatchReason,
    HistoricalSearchStrategy,
    HistoricalTopologyRelation,
    PreparedRAGCaseProjectionV1,
    RAGCandidateRow,
    RAGCaseWindowV1,
    RAGQualityTier,
    RAGScoreComponentsV1,
    TimeRange,
)


class RAGSearchPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def matching_projection_windows(
    query: AuthorizedRAGSearchQuery,
    projection: PreparedRAGCaseProjectionV1,
) -> tuple[RAGCaseWindowV1, ...]:
    """Reapply every strategy hard filter to a hydrated projection."""

    if (
        projection.case_ref.generation_id != query.generation_id
        or projection.profile_id != query.profile_id
        or projection.case_ref.target_comid not in query.candidate_comids
        or not set(projection.parameter_keys) & set(query.intent.parameter_keys)
    ):
        raise RAGSearchPolicyError("historical_projection_outside_authorized_scope")

    strategy = query.intent.strategy
    if strategy in {
        HistoricalSearchStrategy.PARAMETER_SEASON,
        HistoricalSearchStrategy.SEMANTIC_CASE_QUERY,
    } and projection.mode is not _canonical_mode(query):
        raise RAGSearchPolicyError("historical_projection_mode_mismatch")

    if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
        windows = tuple(
            candidate
            for candidate in projection.windows
            if any(
                _positive_overlap(candidate.time_range, requested.time_range)
                for requested in query.query_windows
            )
        )
    elif strategy is HistoricalSearchStrategy.PARAMETER_SEASON:
        requested_seasons = {
            season for window in query.query_windows for season in window.season_buckets
        }
        windows = tuple(
            candidate
            for candidate in projection.windows
            if set(candidate.season_buckets) & requested_seasons
        )
    else:
        windows = projection.windows
    if not windows:
        raise RAGSearchPolicyError("historical_projection_window_mismatch")
    return windows


def rank_projection(
    query: AuthorizedRAGSearchQuery,
    projection: PreparedRAGCaseProjectionV1,
    *,
    lexical_similarity: float,
    vector_similarity: float | None,
    quality_tier: RAGQualityTier,
    original_confidence: float | None,
) -> RAGCandidateRow:
    windows = matching_projection_windows(query, projection)
    lexical = _unit_value(lexical_similarity, "lexical_similarity")
    matched_parameter_count = len(
        set(query.intent.parameter_keys) & set(projection.parameter_keys)
    )
    parameter_ratio = matched_parameter_count / len(query.intent.parameter_keys)
    strategy = query.intent.strategy

    if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
        overlap = _window_overlap_ratio(query.query_windows, windows)
        components = RAGScoreComponentsV1(
            strategy=strategy,
            reasons=(
                HistoricalMatchReason.SAME_COMID,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.WINDOW_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
            same_comid=True,
            parameter_overlap_ratio=parameter_ratio,
            window_overlap_ratio=overlap,
            lexical_similarity=lexical,
        )
        score = 0.75 * overlap + 0.25 * lexical
    elif strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
        scope = _scope_for(query, projection.case_ref.target_comid)
        assert scope.spatial_distance_km is not None
        assert query.spatial_radius_km is not None
        spatial = 1 - min(scope.spatial_distance_km / query.spatial_radius_km, 1)
        components = RAGScoreComponentsV1(
            strategy=strategy,
            reasons=(
                HistoricalMatchReason.NEARBY_COMID,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
            spatial_proximity=spatial,
            parameter_overlap_ratio=parameter_ratio,
            lexical_similarity=lexical,
        )
        score = 0.70 * spatial + 0.30 * lexical
    elif strategy is HistoricalSearchStrategy.PARAMETER_SEASON:
        requested_seasons = {
            season for window in query.query_windows for season in window.season_buckets
        }
        candidate_seasons = {
            season for window in windows for season in window.season_buckets
        }
        season_similarity = len(requested_seasons & candidate_seasons) / len(
            requested_seasons
        )
        duration = max(
            _duration_compatibility(requested, candidate)
            for requested in query.query_windows
            for candidate in windows
            if set(requested.season_buckets) & set(candidate.season_buckets)
        )
        same_comid = projection.case_ref.target_comid == query.current_comid
        components = RAGScoreComponentsV1(
            strategy=strategy,
            reasons=tuple(
                reason
                for reason in (
                    HistoricalMatchReason.SAME_COMID if same_comid else None,
                    HistoricalMatchReason.SEASON_MATCH,
                    HistoricalMatchReason.PARAMETER_OVERLAP,
                    HistoricalMatchReason.LEXICAL_SIMILARITY,
                )
                if reason is not None
            ),
            same_comid=same_comid,
            season_similarity=season_similarity,
            duration_compatibility=duration,
            parameter_overlap_ratio=parameter_ratio,
            lexical_similarity=lexical,
        )
        score = (
            0.35 * float(same_comid)
            + 0.35 * season_similarity
            + 0.20 * duration
            + 0.10 * lexical
        )
    elif strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY:
        vector = _unit_value(vector_similarity, "vector_similarity")
        components = RAGScoreComponentsV1(
            strategy=strategy,
            reasons=(
                HistoricalMatchReason.VECTOR_SIMILARITY,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
            vector_similarity=vector,
            parameter_overlap_ratio=parameter_ratio,
            lexical_similarity=lexical,
        )
        score = 0.85 * vector + 0.15 * lexical
    else:
        scope = _scope_for(query, projection.case_ref.target_comid)
        assert scope.topology_relation is not None
        assert scope.topology_hops is not None
        assert scope.topology_path_distance_km is not None
        assert query.topology_max_hops is not None
        assert query.topology_max_distance_km is not None
        hop = 1 - min(
            (scope.topology_hops - 1) / max(query.topology_max_hops - 1, 1),
            1,
        )
        distance = 1 - min(
            scope.topology_path_distance_km / query.topology_max_distance_km,
            1,
        )
        topology = 0.6 * hop + 0.4 * distance
        upstream = scope.topology_relation is HistoricalTopologyRelation.ANCESTOR
        components = RAGScoreComponentsV1(
            strategy=strategy,
            reasons=(
                HistoricalMatchReason.TOPOLOGY_PROXIMITY,
                HistoricalMatchReason.PARAMETER_OVERLAP,
                HistoricalMatchReason.LEXICAL_SIMILARITY,
            ),
            topology_relation=scope.topology_relation,
            topology_hops=scope.topology_hops,
            topology_path_distance_km=scope.topology_path_distance_km,
            topology_proximity=topology,
            upstream_preference=upstream,
            parameter_overlap_ratio=parameter_ratio,
            lexical_similarity=lexical,
        )
        score = 0.55 * topology + 0.25 * float(upstream) + 0.20 * lexical

    return RAGCandidateRow(
        case_ref=projection.case_ref,
        projection_digest=projection.projection_digest,
        matched_by=components,
        rag_score=_unit_value(score, "rag_score"),
        matched_parameter_count=matched_parameter_count,
        quality_tier=quality_tier,
        original_confidence=original_confidence,
        source_completed_at=projection.source_completed_at,
    )


def stable_candidate_order(candidate: RAGCandidateRow) -> tuple[object, ...]:
    return (
        -candidate.rag_score,
        -candidate.matched_parameter_count,
        0 if candidate.quality_tier is RAGQualityTier.VALID else 1,
        candidate.original_confidence is None,
        -(candidate.original_confidence or 0),
        -_timestamp(candidate.source_completed_at),
        candidate.case_ref.run_id,
        candidate.case_ref.session_id,
        candidate.case_ref.result_item_id,
    )


def _scope_for(query: AuthorizedRAGSearchQuery, comid: str) -> AuthorizedRAGComid:
    scope = next((item for item in query.comid_scope if item.comid == comid), None)
    if scope is None:
        raise RAGSearchPolicyError("historical_projection_outside_authorized_scope")
    return scope


def _canonical_mode(query: AuthorizedRAGSearchQuery):
    from app.domain import CanonicalCaseQueryV1

    return CanonicalCaseQueryV1.model_validate_json(query.canonical_query).mode


def _duration_compatibility(
    requested: RAGCaseWindowV1,
    candidate: RAGCaseWindowV1,
) -> float:
    return min(requested.duration_seconds, candidate.duration_seconds) / max(
        requested.duration_seconds,
        candidate.duration_seconds,
    )


def _window_overlap_ratio(
    requested: tuple[RAGCaseWindowV1, ...],
    candidates: tuple[RAGCaseWindowV1, ...],
) -> float:
    requested_ranges = _merge_ranges(tuple(item.time_range for item in requested))
    intersections = tuple(
        TimeRange(
            start=max(requested_range.start, candidate.time_range.start),
            end=min(requested_range.end, candidate.time_range.end),
        )
        for requested_range in requested_ranges
        for candidate in candidates
        if _positive_overlap(requested_range, candidate.time_range)
    )
    covered = sum(
        (item.end - item.start).total_seconds()
        for item in _merge_ranges(intersections)
    )
    total = sum(
        (item.end - item.start).total_seconds() for item in requested_ranges
    )
    if total <= 0:
        raise RAGSearchPolicyError("historical_query_window_invalid")
    return _unit_value(covered / total, "window_overlap_ratio")


def _merge_ranges(ranges: tuple[TimeRange, ...]) -> tuple[TimeRange, ...]:
    ordered = sorted(ranges, key=lambda item: (item.start, item.end))
    merged: list[TimeRange] = []
    for item in ordered:
        if not merged or item.start > merged[-1].end:
            merged.append(item)
            continue
        merged[-1] = TimeRange(
            start=merged[-1].start,
            end=max(merged[-1].end, item.end),
        )
    return tuple(merged)


def _positive_overlap(first: TimeRange, second: TimeRange) -> bool:
    return max(first.start, second.start) < min(first.end, second.end)


def _unit_value(value: float | None, field_name: str) -> float:
    if value is None or not isfinite(value) or not 0 <= value <= 1:
        raise RAGSearchPolicyError(f"historical_{field_name}_invalid")
    return float(value)


def _timestamp(value: datetime) -> float:
    timestamp = value.timestamp()
    if not isfinite(timestamp):
        raise RAGSearchPolicyError("historical_completion_time_invalid")
    return timestamp
