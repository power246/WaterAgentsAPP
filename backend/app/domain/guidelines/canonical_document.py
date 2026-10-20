from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import (
    NormalizedText, canonical_text, digest, require_safe_model_text, unique_texts,
)
from app.domain.guidelines.content import GuidelineContentV1
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.guidelines.model_view import GuidelineModelViewV1, project_model_view
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry

MAX_GUIDELINE_DOCUMENT_BYTES = 24_000


class CanonicalGuidelineDocumentV1(DomainModel):
    kind: Literal["guideline_document"] = "guideline_document"
    schema_version: Literal[1] = 1
    template_version: Literal[1] = 1
    profile_id: Annotated[str, Field(pattern=r"^guideline:sha256:[0-9a-f]{64}$")]
    instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)]
    model_view: GuidelineModelViewV1
    parameter_names: Annotated[tuple[NormalizedText, ...], Field(min_length=1)]
    scenario_tags: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=48)], ...],
        Field(max_length=12),
    ] = ()
    keywords: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=64)], ...],
        Field(max_length=24),
    ] = ()

    @field_validator("parameter_names", "scenario_tags", "keywords", mode="before")
    @classmethod
    def canonical_sets(cls, value):
        return unique_texts(value)

    @model_validator(mode="after")
    def safe_complete_input(self) -> Self:
        require_safe_model_text(self.canonical_text)
        if len(self.canonical_text.encode("utf-8")) > MAX_GUIDELINE_DOCUMENT_BYTES:
            raise ValueError("canonical guideline document exceeds 24000 UTF-8 bytes")
        return self

    @property
    def canonical_text(self) -> str:
        return canonical_text(self.model_dump(mode="json"))

    @property
    def canonical_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def require_profile(self, profile: GuidelineEmbeddingProfileV1) -> None:
        if self.profile_id != profile.profile_id or self.instructions != profile.document_instructions:
            raise ValueError("document input does not match guideline profile")

    @classmethod
    def from_content(
        cls, content: GuidelineContentV1, profile: GuidelineEmbeddingProfileV1,
        registry: ParameterCapabilityRegistry,
    ) -> Self:
        content.require_registered_parameters(registry)
        names = tuple(sorted({registry.get(key).canonical_name for key in (
            *content.parameter_keys, *content.related_parameter_keys,
        )}))
        return cls(
            profile_id=profile.profile_id, instructions=profile.document_instructions,
            model_view=project_model_view(content), parameter_names=names,
            scenario_tags=content.scenario_tags, keywords=content.keywords,
        )
