from __future__ import annotations

from math import isfinite, sqrt
from typing import Sequence

from pydantic import ValidationError

from app.domain import (
    CanonicalCaseQueryV1,
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingVector,
)

MAX_EMBEDDING_BATCH_ITEMS = 2_048
MIN_EMBEDDING_VECTOR_NORM = 1e-12
MAX_EMBEDDING_VECTOR_NORM = 1e6


class EmbeddingContractError(ValueError):
    """A canonical input or vector batch violates the provider-neutral port."""


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
    if len(vectors) != expected_count:
        raise EmbeddingContractError("embedding output count does not match input")

    validated: list[EmbeddingVector] = []
    for values in vectors:
        if len(values) != profile.output_dimensions:
            raise EmbeddingContractError(
                "embedding output dimensions do not match profile"
            )
        numeric: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingContractError(
                    "embedding output contains a non-numeric value"
                )
            converted = float(value)
            if not isfinite(converted):
                raise EmbeddingContractError(
                    "embedding output contains a non-finite value"
                )
            numeric.append(converted)
        norm = sqrt(sum(value * value for value in numeric))
        if not MIN_EMBEDDING_VECTOR_NORM <= norm <= MAX_EMBEDDING_VECTOR_NORM:
            raise EmbeddingContractError("embedding output norm is outside policy")
        validated.append(EmbeddingVector(values=tuple(numeric)))
    return EmbeddingBatch(profile=profile, vectors=tuple(validated))
