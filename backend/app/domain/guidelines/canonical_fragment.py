from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, canonical_text, digest, require_safe_model_text
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1


class CanonicalGuidelineFragmentV3(DomainModel):
    kind: Literal["guideline_relation"] = "guideline_relation"
    schema_version: Literal[3] = 3
    template_version: Literal[3] = 3
    profile_id: Annotated[str, Field(pattern=r"^guideline:sha256:[0-9a-f]{64}$")]
    instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)]
    relation: Annotated[NormalizedText, Field(min_length=1, max_length=6000)]

    @model_validator(mode="after")
    def safe_relation(self) -> Self:
        if not self.relation.strip():
            raise ValueError("relation must not be blank")
        require_safe_model_text(self.canonical_text)
        return self

    @property
    def canonical_text(self) -> str:
        return canonical_text(self.model_dump(mode="json"))

    @property
    def canonical_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def require_profile(self, profile: GuidelineEmbeddingProfileV1) -> None:
        if (profile.document_template_version != 3 or self.profile_id != profile.profile_id
                or self.instructions != profile.document_instructions):
            raise ValueError("relation input does not match guideline profile")
