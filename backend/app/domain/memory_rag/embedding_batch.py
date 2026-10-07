from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel
from app.domain.memory_rag.embedding_profile import EmbeddingProfile
from app.domain.memory_rag.embedding_vector import EmbeddingVector


class EmbeddingBatch(DomainModel):
    profile: EmbeddingProfile
    vectors: tuple[EmbeddingVector, ...]

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        if any(
            len(vector.values) != self.profile.output_dimensions
            for vector in self.vectors
        ):
            raise ValueError("embedding vector dimensions do not match profile")
        return self
