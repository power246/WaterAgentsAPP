import pytest

from app.application.transcript_memory.search_policies import (
    rank_projection,
    stable_candidate_order,
)
from app.domain import HistoricalSearchStrategy, RAGQualityTier
from tests.application.transcript_memory.test_search_policies import (
    _projection,
    _query,
)


def test_strategy_score_goldens_lock_components_and_formulas() -> None:
    projection = _projection()
    expected = {
        HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW: 0.80,
        HistoricalSearchStrategy.NEARBY_COMID_PARAMETER: 0.62,
        HistoricalSearchStrategy.PARAMETER_SEASON: 0.92,
        HistoricalSearchStrategy.SEMANTIC_CASE_QUERY: 0.71,
        HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER: 0.796,
    }
    for strategy, score in expected.items():
        candidate = rank_projection(
            _query(strategy),
            projection,
            lexical_similarity=0.2,
            vector_similarity=(
                0.8
                if strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
                else None
            ),
            quality_tier=RAGQualityTier.VALID,
            original_confidence=0.8,
        )
        assert candidate.rag_score == pytest.approx(score)
        assert candidate.matched_parameter_count == 1
        assert candidate.matched_by.strategy is strategy


def test_stable_order_is_insertion_independent_and_uses_quality_confidence_identity() -> None:
    projection = _projection()
    base = rank_projection(
        _query(HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
        projection,
        lexical_similarity=0.2,
        vector_similarity=None,
        quality_tier=RAGQualityTier.VALID,
        original_confidence=0.8,
    )
    suspicious = base.model_copy(
        update={
            "case_ref": base.case_ref.model_copy(update={"run_id": "run-b"}),
            "quality_tier": RAGQualityTier.SUSPICIOUS,
            "original_confidence": 1.0,
        }
    )
    lower_confidence = base.model_copy(
        update={
            "case_ref": base.case_ref.model_copy(update={"run_id": "run-a"}),
            "original_confidence": 0.7,
        }
    )

    expected = (base, lower_confidence, suspicious)
    assert tuple(sorted(reversed(expected), key=stable_candidate_order)) == expected


def test_non_finite_signal_invalidates_ranking_instead_of_dropping_one_row() -> None:
    with pytest.raises(ValueError, match="lexical_similarity_invalid"):
        rank_projection(
            _query(HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW),
            _projection(),
            lexical_similarity=float("nan"),
            vector_similarity=None,
            quality_tier=RAGQualityTier.VALID,
            original_confidence=0.8,
        )
