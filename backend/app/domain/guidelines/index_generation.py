from typing import Literal
from uuid import UUID

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Revision
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1


class GuidelineIndexGeneration(DomainModel):
    generation_id: UUID
    profile: GuidelineEmbeddingProfileV1
    state: Literal["building", "active", "retired", "failed"]
    catalog_revision: Revision
