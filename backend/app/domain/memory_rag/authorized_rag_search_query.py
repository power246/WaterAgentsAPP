from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.domain.memory_rag.historical_search_intent import HistoricalSearchIntentV1
from app.domain.memory_rag.historical_search_strategy import HistoricalSearchStrategy


class AuthorizedRAGSearchQuery(DomainModel):
    generation_id: NonEmptyString
    profile_id: NonEmptyString
    intent: HistoricalSearchIntentV1
    current_comid: NonEmptyString
    candidate_comids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    canonical_query_digest: NonEmptyString
    query_embedding: EmbeddingVector | None = None

    @field_validator("candidate_comids")
    @classmethod
    def validate_candidate_comids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="authorized candidate COMIDs")

    @model_validator(mode="after")
    def validate_embedding_use(self) -> Self:
        semantic = self.intent.strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
        if semantic != (self.query_embedding is not None):
            raise ValueError("semantic strategy and query embedding must agree")
        return self
