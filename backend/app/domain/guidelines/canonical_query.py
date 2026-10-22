from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import (
    NormalizedText, canonical_text, digest, require_safe_model_text, unique_parameters, unique_texts,
)
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.mode import InferenceMode

MAX_GUIDELINE_QUERY_BYTES = 8_192


class CanonicalGuidelineQueryV1(DomainModel):
    kind: Literal["guideline_query"] = "guideline_query"
    schema_version: Literal[1] = 1
    template_version: Literal[1] = 1
    profile_id: Annotated[str, Field(pattern=r"^guideline:sha256:[0-9a-f]{64}$")]
    instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)]
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    mode: InferenceMode
    question: NormalizedText | None = None
    scenario_tags: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=48)], ...],
        Field(max_length=12),
    ] = ()
    task_facts: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=240)], ...],
        Field(max_length=24),
    ] = ()
    input_reduced: bool = False

    @field_validator("parameter_keys")
    @classmethod
    def canonical_parameters(cls, value):
        return unique_parameters(value)

    @field_validator("scenario_tags", "task_facts", mode="before")
    @classmethod
    def canonical_sets(cls, value):
        return unique_texts(value)

    @model_validator(mode="after")
    def bounded_safe_query(self) -> Self:
        require_safe_model_text(self.canonical_text)
        if len(self.canonical_text.encode("utf-8")) > MAX_GUIDELINE_QUERY_BYTES:
            raise ValueError("canonical guideline query exceeds 8192 UTF-8 bytes")
        return self

    @property
    def canonical_text(self) -> str:
        return canonical_text(self.model_dump(mode="json"))

    @property
    def canonical_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def require_profile(self, profile: GuidelineEmbeddingProfileV1) -> None:
        if self.profile_id != profile.profile_id or self.instructions != profile.query_instructions:
            raise ValueError("query input does not match guideline profile")

    def require_registered_parameters(self, registry: ParameterCapabilityRegistry) -> None:
        if any(registry.get(key) is None for key in self.parameter_keys):
            raise ValueError("query contains an unregistered parameter")
