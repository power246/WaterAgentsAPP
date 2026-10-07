from hashlib import sha256
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.memory_rag.authorized_rag_comid import AuthorizedRAGComid
from app.domain.memory_rag.canonical_case_query import CanonicalCaseQueryV1
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.domain.memory_rag.historical_search_intent import HistoricalSearchIntentV1
from app.domain.memory_rag.historical_search_strategy import HistoricalSearchStrategy
from app.domain.memory_rag.rag_case_window import RAGCaseWindowV1


class AuthorizedRAGSearchQuery(DomainModel):
    generation_id: NonEmptyString
    profile_id: NonEmptyString
    intent: HistoricalSearchIntentV1
    current_comid: NonEmptyString
    candidate_comids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    comid_scope: Annotated[tuple[AuthorizedRAGComid, ...], Field(min_length=1)]
    query_windows: Annotated[tuple[RAGCaseWindowV1, ...], Field(min_length=1)]
    canonical_query: Annotated[NonEmptyString, Field(max_length=16_384)]
    canonical_query_digest: NonEmptyString
    spatial_radius_km: Annotated[DomainFloat, Field(gt=0, le=25)] | None = None
    topology_max_hops: Annotated[int, Field(strict=True, ge=1)] | None = None
    topology_max_distance_km: Annotated[DomainFloat, Field(gt=0)] | None = None
    semantic_min_similarity: Annotated[
        DomainFloat,
        Field(ge=0, le=1),
    ] | None = None
    query_embedding: EmbeddingVector | None = None

    @field_validator("candidate_comids")
    @classmethod
    def validate_candidate_comids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="authorized candidate COMIDs")

    @model_validator(mode="after")
    def validate_embedding_use(self) -> Self:
        strategy = self.intent.strategy
        semantic = strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
        if semantic != (self.query_embedding is not None) or semantic != (
            self.semantic_min_similarity is not None
        ):
            raise ValueError(
                "semantic strategy, threshold, and query embedding must agree"
            )
        canonical = CanonicalCaseQueryV1.model_validate_json(self.canonical_query)
        if canonical.canonical_text != self.canonical_query:
            raise ValueError("authorized query must use canonical case-query bytes")
        expected_digest = f"sha256:{sha256(self.canonical_query.encode('ascii')).hexdigest()}"
        if self.canonical_query_digest != expected_digest:
            raise ValueError("authorized canonical query digest does not match its bytes")
        if canonical.requested_parameter_keys != tuple(
            sorted(
                self.intent.parameter_keys,
                key=lambda item: (item.code_system, item.code),
            )
        ):
            raise ValueError("authorized canonical parameters must match the intent")
        if self.intent.window_ids != tuple(window.window_id for window in self.query_windows):
            raise ValueError("authorized query windows must match the intent")
        if canonical.window_descriptors != tuple(
            sorted(
                (window.canonical_descriptor() for window in self.query_windows),
                key=lambda item: (
                    item.window_kind.value,
                    item.duration_days,
                    item.local_months,
                    tuple(season.value for season in item.season_buckets),
                    item.resolution.value,
                ),
            )
        ):
            raise ValueError("authorized canonical windows must match the query windows")
        scope_comids = tuple(item.comid for item in self.comid_scope)
        ensure_unique_tuple(scope_comids, field_name="authorized COMID scope")
        if self.candidate_comids != scope_comids:
            raise ValueError("candidate COMIDs must match the ordered authorized scope")

        if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
            if self.candidate_comids != (self.current_comid,):
                raise ValueError("exact search must authorize only the current COMID")
        elif strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
            if self.spatial_radius_km is None:
                raise ValueError("nearby search requires its saved-graph radius")
            if any(
                item.comid == self.current_comid
                or item.spatial_distance_km is None
                or item.spatial_distance_km > self.spatial_radius_km
                for item in self.comid_scope
            ):
                raise ValueError("nearby COMIDs must be distinct and within the radius")
        elif strategy is HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER:
            if self.topology_max_hops is None or self.topology_max_distance_km is None:
                raise ValueError("topology search requires saved graph limits")
            if any(
                item.comid == self.current_comid
                or item.topology_relation is None
                or item.topology_hops is None
                or item.topology_path_distance_km is None
                or item.topology_hops > self.topology_max_hops
                or item.topology_path_distance_km > self.topology_max_distance_km
                for item in self.comid_scope
            ):
                raise ValueError("topology COMIDs must use bounded saved paths")
        if strategy is not HistoricalSearchStrategy.NEARBY_COMID_PARAMETER and (
            self.spatial_radius_km is not None
        ):
            raise ValueError("only nearby search may carry a spatial radius")
        if strategy is not HistoricalSearchStrategy.TOPOLOGY_NEIGHBORHOOD_PARAMETER and (
            self.topology_max_hops is not None
            or self.topology_max_distance_km is not None
        ):
            raise ValueError("only topology search may carry topology limits")
        return self
