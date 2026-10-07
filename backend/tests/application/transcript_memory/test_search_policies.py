from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.transcript_memory.search_policies import (
    RAGSearchPolicyError,
    matching_projection_windows,
)
from app.domain import (
    AuthorizedRAGComid,
    AuthorizedRAGSearchQuery,
    CanonicalCaseQueryV1,
    EmbeddingVector,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    HistoricalTopologyRelation,
    InferenceMode,
    RAGCaseWindowV1,
    TimeRange,
)
from tests.persistence.rag_test_support import (
    TranscriptSourceFixture,
    embedding_profile,
    ready_projection,
)


def _projection():
    source = TranscriptSourceFixture(
        run_id=UUID("10000000-0000-4000-8000-000000000001"),
        session_id=UUID("20000000-0000-4000-8000-000000000001"),
        analysis_context_item_id=UUID("30000000-0000-4000-8000-000000000001"),
        fragment_item_id=UUID("40000000-0000-4000-8000-000000000001"),
        result_item_id=UUID("50000000-0000-4000-8000-000000000001"),
        completed_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    return ready_projection(
        source,
        "60000000-0000-4000-8000-000000000001",
        embedding_profile(),
    )[0]


def _query(
    strategy: HistoricalSearchStrategy,
    *,
    projection=None,
    comid: str | None = None,
    window: RAGCaseWindowV1 | None = None,
):
    projection = projection or _projection()
    window = window or projection.windows[0]
    current = comid or projection.case_ref.target_comid
    if strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
        current = "87654321"
        scope = (
            AuthorizedRAGComid(
                comid=projection.case_ref.target_comid,
                spatial_distance_km=5,
            ),
        )
        extras = {"spatial_radius_km": 25}
    elif strategy is HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER:
        current = "87654321"
        scope = (
            AuthorizedRAGComid(
                comid=projection.case_ref.target_comid,
                topology_relation=HistoricalTopologyRelation.ANCESTOR,
                topology_hops=1,
                topology_path_distance_km=5,
            ),
        )
        extras = {"topology_max_hops": 5, "topology_max_distance_km": 25}
    else:
        scope = (AuthorizedRAGComid(comid=projection.case_ref.target_comid),)
        extras = {}
    canonical = CanonicalCaseQueryV1(
        mode=projection.mode,
        requested_parameter_keys=projection.parameter_keys,
        window_descriptors=(window.canonical_descriptor(),),
        normalized_intent_tokens=("analyze_current_state",),
    )
    return AuthorizedRAGSearchQuery(
        generation_id=projection.case_ref.generation_id,
        profile_id=projection.profile_id,
        intent=HistoricalSearchIntentV1(
            strategy=strategy,
            parameter_keys=projection.parameter_keys,
            window_ids=(window.window_id,),
        ),
        current_comid=current,
        candidate_comids=tuple(item.comid for item in scope),
        comid_scope=scope,
        query_windows=(window,),
        canonical_query=canonical.canonical_text,
        canonical_query_digest=canonical.canonical_digest,
        query_embedding=(
            EmbeddingVector(values=(0.1, 0.1, 0.1))
            if strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
            else None
        ),
        semantic_min_similarity=(
            0.75
            if strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
            else None
        ),
        **extras,
    )


@pytest.mark.parametrize("strategy", tuple(HistoricalSearchStrategy))
def test_each_strategy_reapplies_hard_filters_before_ranking(strategy) -> None:
    projection = _projection()

    assert matching_projection_windows(_query(strategy), projection)

    outside = projection.model_copy(
        update={
            "case_ref": projection.case_ref.model_copy(
                update={"target_comid": "99999999"}
            )
        }
    )
    with pytest.raises(RAGSearchPolicyError, match="outside_authorized_scope"):
        matching_projection_windows(_query(strategy), outside)


def test_exact_requires_positive_utc_overlap_and_season_requires_shared_bucket() -> None:
    projection = _projection()
    historical = projection.windows[0]
    later = historical.model_copy(
        update={
            "window_id": "current-later",
            "time_range": TimeRange(
                start=historical.time_range.end,
                end=historical.time_range.end + timedelta(days=7),
            ),
            "local_months": (8,),
            "season_buckets": ("JJA",),
            "season_year": 2026,
        }
    )
    with pytest.raises(RAGSearchPolicyError, match="window_mismatch"):
        matching_projection_windows(
            _query(HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW, window=later),
            projection,
        )
    winter = later.model_copy(
        update={"local_months": (1,), "season_buckets": ("DJF",)}
    )
    with pytest.raises(RAGSearchPolicyError, match="window_mismatch"):
        matching_projection_windows(
            _query(HistoricalSearchStrategy.PARAMETER_SEASON, window=winter),
            projection,
        )


def test_semantic_hard_filter_requires_same_mode() -> None:
    projection = _projection()
    mismatched = projection.model_copy(update={"mode": InferenceMode.GAP_FILLING})

    with pytest.raises(RAGSearchPolicyError, match="mode_mismatch"):
        matching_projection_windows(
            _query(HistoricalSearchStrategy.SEMANTIC_CASE_QUERY),
            mismatched,
        )
