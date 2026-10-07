from hashlib import sha256
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.memory_rag.embedding_distance_metric import EmbeddingDistanceMetric

_AUTO_PROFILE_ID = "__auto_embedding_profile_id__"


class EmbeddingProfile(DomainModel):
    profile_id: NonEmptyString = _AUTO_PROFILE_ID
    provider: NonEmptyString
    model: NonEmptyString
    output_dimensions: Annotated[int, Field(strict=True, ge=1)]
    max_input_tokens: Annotated[int, Field(strict=True, ge=1)]
    distance_metric: Literal[EmbeddingDistanceMetric.COSINE] = EmbeddingDistanceMetric.COSINE
    semantic_min_similarity: Annotated[DomainFloat, Field(ge=0, le=1)]
    canonical_query_schema_version: Literal[1] = 1
    canonical_template_version: Literal[1] = 1
    projection_schema_version: Literal[1] = 1
    profile_digest: NonEmptyString = _AUTO_PROFILE_ID

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        identity = {
            "canonical_query_schema_version": self.canonical_query_schema_version,
            "canonical_template_version": self.canonical_template_version,
            "distance_metric": self.distance_metric.value,
            "model": self.model,
            "max_input_tokens": self.max_input_tokens,
            "output_dimensions": self.output_dimensions,
            "projection_schema_version": self.projection_schema_version,
            "provider": self.provider,
            "semantic_min_similarity": self.semantic_min_similarity,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected = f"sha256:{sha256(canonical).hexdigest()}"
        for field_name in ("profile_id", "profile_digest"):
            value = getattr(self, field_name)
            if value == _AUTO_PROFILE_ID:
                object.__setattr__(self, field_name, expected)
            elif value != expected:
                raise ValueError(f"{field_name} does not match embedding profile digest")
        return self
