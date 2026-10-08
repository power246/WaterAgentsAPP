from __future__ import annotations

import logging
import re
from collections import Counter
from math import isfinite
from threading import Lock

from app.domain import RAGJobCounts


logger = logging.getLogger("wateragents.rag")

_EMBEDDING_STAGES = frozenset({"indexing", "search"})
_EMBEDDING_OUTCOMES = frozenset(
    {"cancelled", "completed", "failed", "invalid", "rate_limited", "timeout", "unavailable"}
)
_SEARCH_STRATEGIES = frozenset(
    {
        "exact_comid_parameter_window",
        "nearby_comid_parameter",
        "parameter_season",
        "semantic_case_query",
        "topology_neighborhood_parameter",
    }
)
_SEARCH_OUTCOMES = frozenset(
    {
        "available",
        "embedding_protocol_invalid",
        "embedding_unavailable",
        "empty",
        "historical_index_unavailable",
        "historical_source_invalid",
        "index_building",
        "index_invalid",
        "invalid",
    }
)
_GENERATION_STATUSES = frozenset({"active", "building", "failed", "retired"})
_PROFILE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class RAGRuntimeMetrics:
    """Process-local bounded metrics for the single M8 runtime."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._embedding: Counter[tuple[str, str]] = Counter()
        self._search: Counter[tuple[str, str]] = Counter()
        self._search_candidates: Counter[str] = Counter()
        self._embedding_latency_seconds: Counter[str] = Counter()
        self._search_latency_seconds: Counter[str] = Counter()
        self._hydration_invalid: Counter[str] = Counter()
        self._generation: dict[str, object] = {}

    def observe_embedding(
        self,
        *,
        stage: str,
        outcome: str,
        latency_seconds: float,
    ) -> None:
        stage = stage if stage in _EMBEDDING_STAGES else "search"
        outcome = outcome if outcome in _EMBEDDING_OUTCOMES else "failed"
        latency_seconds = _nonnegative(latency_seconds)
        with self._lock:
            self._embedding[(stage, outcome)] += 1
            self._embedding_latency_seconds[stage] += latency_seconds
        logger.info(
            "Embedding operation completed.",
            extra={
                "event_name": "rag_embedding_completed",
                "embedding_stage": stage,
                "outcome": outcome,
                "duration_ms": round(latency_seconds * 1000, 3),
            },
        )

    def observe_search_candidates(
        self,
        *,
        strategy: str,
        candidate_count: int,
    ) -> None:
        strategy = strategy if strategy in _SEARCH_STRATEGIES else "semantic_case_query"
        candidate_count = min(max(int(candidate_count), 0), 50)
        with self._lock:
            self._search_candidates[strategy] += candidate_count
        logger.info(
            "Historical search candidates selected.",
            extra={
                "event_name": "rag_search_candidates_selected",
                "strategy": strategy,
                "candidate_count": candidate_count,
            },
        )

    def observe_search(
        self,
        *,
        strategy: str,
        outcome: str,
        latency_seconds: float,
        result_count: int,
    ) -> None:
        strategy = strategy if strategy in _SEARCH_STRATEGIES else "semantic_case_query"
        outcome = outcome if outcome in _SEARCH_OUTCOMES else "invalid"
        result_count = min(max(int(result_count), 0), 5)
        latency_seconds = _nonnegative(latency_seconds)
        with self._lock:
            self._search[(strategy, outcome)] += 1
            self._search_latency_seconds[strategy] += latency_seconds
        logger.info(
            "Historical search completed.",
            extra={
                "event_name": "rag_search_completed",
                "strategy": strategy,
                "outcome": outcome,
                "result_count": result_count,
                "duration_ms": round(latency_seconds * 1000, 3),
            },
        )

    def observe_hydration_invalid(self, code: str) -> None:
        code = code if code == "historical_source_invalid" else "historical_source_invalid"
        with self._lock:
            self._hydration_invalid[code] += 1
        logger.warning(
            "Historical source hydration was rejected.",
            extra={
                "event_name": "rag_hydration_invalid",
                "error_code": code,
            },
        )

    def observe_generation(
        self,
        *,
        status: str,
        age_seconds: float,
        counts: RAGJobCounts,
        lag_seconds: float,
        active_profile_digest: str | None,
    ) -> None:
        status = status if status in _GENERATION_STATUSES else "failed"
        active_profile_digest = (
            active_profile_digest
            if active_profile_digest is not None
            and _PROFILE_DIGEST.fullmatch(active_profile_digest)
            else None
        )
        age_seconds = _nonnegative(age_seconds)
        lag_seconds = _nonnegative(lag_seconds)
        latest = {
            "status": status,
            "age_seconds": age_seconds,
            "pending": counts.pending,
            "leased": counts.leased,
            "retry_wait": counts.retry_wait,
            "failed": counts.failed,
            "lag_seconds": lag_seconds,
            "active_profile_digest": active_profile_digest,
        }
        with self._lock:
            self._generation = latest
        logger.info(
            "RAG generation health observed.",
            extra={
                "event_name": "rag_generation_health",
                "generation_status": status,
                "generation_age_seconds": round(age_seconds, 3),
                "job_pending": counts.pending,
                "job_leased": counts.leased,
                "job_retry_wait": counts.retry_wait,
                "job_failed": counts.failed,
                "job_lag_seconds": round(lag_seconds, 3),
                "active_profile_digest": active_profile_digest,
            },
        )

    def observe_reconciliation_failure(self, code: str) -> None:
        logger.warning(
            "RAG reconciliation failed and will be retried.",
            extra={
                "event_name": "rag_reconciliation_failed",
                "error_code": code,
            },
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "embedding": tuple(
                    sorted(
                        (stage, outcome, count)
                        for (stage, outcome), count in self._embedding.items()
                    )
                ),
                "embedding_latency_seconds": tuple(
                    sorted(self._embedding_latency_seconds.items())
                ),
                "search_candidates": tuple(sorted(self._search_candidates.items())),
                "search": tuple(
                    sorted(
                        (strategy, outcome, count)
                        for (strategy, outcome), count in self._search.items()
                    )
                ),
                "search_latency_seconds": tuple(
                    sorted(self._search_latency_seconds.items())
                ),
                "hydration_invalid": tuple(sorted(self._hydration_invalid.items())),
                "generation": dict(self._generation),
            }


def _nonnegative(value: float) -> float:
    numeric = float(value)
    return max(numeric, 0) if isfinite(numeric) else 0
