from math import sqrt
from typing import Self

from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.memory_rag.embedding_vector import EmbeddingVector


class GuidelineEmbeddingBatch(DomainModel):
    profile: GuidelineEmbeddingProfileV1
    vectors: tuple[EmbeddingVector, ...]

    @model_validator(mode="after")
    def compatible_vectors(self) -> Self:
        for vector in self.vectors:
            if len(vector.values) != self.profile.output_dimensions:
                raise ValueError("guideline vector dimensions do not match profile")
            norm = sqrt(sum(value * value for value in vector.values))
            if not 1e-12 <= norm <= 1e6:
                raise ValueError("guideline vector norm is outside the allowed range")
        return self
