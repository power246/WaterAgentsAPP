from collections.abc import Sequence
from math import isfinite, sqrt

from app.domain.memory_rag.embedding_vector import EmbeddingVector

MAX_EMBEDDING_BATCH_ITEMS = 2_048
MIN_EMBEDDING_VECTOR_NORM = 1e-12
MAX_EMBEDDING_VECTOR_NORM = 1e6


class EmbeddingContractError(ValueError):
    """An embedding input or vector batch violates its port contract."""


def validated_embedding_vectors(
    vectors: Sequence[Sequence[object]], *, dimensions: int, expected_count: int,
) -> tuple[EmbeddingVector, ...]:
    if len(vectors) != expected_count:
        raise EmbeddingContractError("embedding output count does not match input")
    validated = []
    for values in vectors:
        if len(values) != dimensions:
            raise EmbeddingContractError("embedding output dimensions do not match profile")
        numeric = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingContractError("embedding output contains a non-numeric value")
            try:
                converted = float(value)
            except OverflowError:
                raise EmbeddingContractError("embedding output contains a non-finite value") from None
            if not isfinite(converted):
                raise EmbeddingContractError("embedding output contains a non-finite value")
            numeric.append(converted)
        norm = sqrt(sum(value * value for value in numeric))
        if not MIN_EMBEDDING_VECTOR_NORM <= norm <= MAX_EMBEDDING_VECTOR_NORM:
            raise EmbeddingContractError("embedding output norm is outside policy")
        validated.append(EmbeddingVector(values=tuple(numeric)))
    return tuple(validated)
