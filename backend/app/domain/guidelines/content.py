from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.guidelines.canonical import NormalizedText
from app.domain.guidelines.content_fields import _GuidelineContentFields
from app.domain.guidelines.source_kind import GuidelineSourceKind
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.domain.hydrology.parameter_key import ParameterKey

MAX_PUBLISHED_BODY_BYTES = 6_000


class GuidelineContentV1(_GuidelineContentFields):
    title: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    summary: Annotated[NormalizedText, Field(min_length=1, max_length=240)]
    parameter_keys: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    language: Literal["zh", "en", "mixed"]
    source_kind: GuidelineSourceKind
    body_markdown: Annotated[NormalizedText, Field(min_length=1)]

    @model_validator(mode="after")
    def publication_constraints(self) -> Self:
        for name in ("title", "summary", "body_markdown"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if len(self.body_markdown.encode("utf-8")) > MAX_PUBLISHED_BODY_BYTES:
            raise ValueError("body_markdown exceeds 6000 UTF-8 bytes")
        if self.source_kind is GuidelineSourceKind.REFERENCE_DERIVED and not self.sources:
            raise ValueError("sources are required for reference-derived content")
        return self

    def require_registered_parameters(self, registry: ParameterCapabilityRegistry) -> None:
        for name in ("parameter_keys", "related_parameter_keys"):
            if any(registry.get(key) is None for key in getattr(self, name)):
                raise ValueError(f"{name} contains an unregistered parameter")
