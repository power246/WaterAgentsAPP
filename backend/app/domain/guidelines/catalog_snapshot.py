from typing import Self
from uuid import UUID

from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Revision
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1


class GuidelineCatalogSnapshot(DomainModel):
    revision: Revision
    generation_id: UUID | None = None
    profile: GuidelineEmbeddingProfileV1 | None = None

    @model_validator(mode="after")
    def consistent_readiness(self) -> Self:
        if (self.generation_id is None) != (self.profile is None):
            raise ValueError("catalog generation and profile must be available together")
        return self
