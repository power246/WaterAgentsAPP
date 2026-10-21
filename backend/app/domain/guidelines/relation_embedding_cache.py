from typing import Self

from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.guidelines.embedding_batch import GuidelineEmbeddingBatch
from app.domain.memory_rag.embedding_vector import EmbeddingVector


class GuidelineRelationEmbeddingCache(DomainModel):
    profile: GuidelineEmbeddingProfileV1
    entries: tuple[tuple[Digest, EmbeddingVector], ...] = ()

    @model_validator(mode="after")
    def unique_validated_vectors(self) -> Self:
        ids = tuple(key for key, _ in self.entries)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("relation embedding cache keys must be unique and sorted")
        GuidelineEmbeddingBatch(profile=self.profile, vectors=tuple(v for _, v in self.entries))
        return self
