from __future__ import annotations

from typing import Sequence

from pydantic import ValidationError

from app.domain import (
    CanonicalCaseQueryV1,
    EmbeddingBatch,
    EmbeddingProfile,
)
from app.application.embedding_validation import (
    EmbeddingContractError, MAX_EMBEDDING_BATCH_ITEMS, validated_embedding_vectors,
)


def validate_canonical_embedding_texts(
    profile: EmbeddingProfile,
    texts: tuple[str, ...],
) -> None:
    if len(texts) > MAX_EMBEDDING_BATCH_ITEMS:
        raise EmbeddingContractError("embedding batch exceeds the item limit")
    for text in texts:
        if not isinstance(text, str):
            raise EmbeddingContractError("embedding input must be canonical text")
        try:
            query = CanonicalCaseQueryV1.model_validate_json(text)
        except (ValidationError, ValueError) as error:
            raise EmbeddingContractError(
                "embedding input must be canonical case-query text"
            ) from error
        if query.canonical_text != text:
            raise EmbeddingContractError(
                "embedding input must use canonical case-query bytes"
            )
        if (
            query.schema_version != profile.canonical_query_schema_version
            or query.template_version != profile.canonical_template_version
            or query.projection_schema_version != profile.projection_schema_version
        ):
            raise EmbeddingContractError(
                "embedding input versions do not match the selected profile"
            )


def validated_embedding_batch(
    profile: EmbeddingProfile,
    vectors: Sequence[Sequence[object]],
    *,
    expected_count: int,
) -> EmbeddingBatch:
    return EmbeddingBatch(profile=profile, vectors=validated_embedding_vectors(
        vectors, dimensions=profile.output_dimensions, expected_count=expected_count,
    ))
