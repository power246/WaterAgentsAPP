from typing import Annotated

from pydantic import Field, field_validator

from app.domain.guidelines.content import GuidelineContentV1
from app.domain.guidelines.content_fields import _GuidelineContentFields
from app.domain.guidelines.draft_source_ref import GuidelineDraftSourceRef
from app.domain.guidelines.model_view import project_model_view
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry

MAX_DRAFT_BODY_BYTES = 24_000


class GuidelineDraftV1(_GuidelineContentFields):
    sources: Annotated[tuple[GuidelineDraftSourceRef, ...], Field(max_length=16)] = ()

    @field_validator("body_markdown")
    @classmethod
    def bounded_body(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > MAX_DRAFT_BODY_BYTES:
            raise ValueError("body_markdown exceeds 24000 UTF-8 bytes")
        return value

    def publication_content(self, registry: ParameterCapabilityRegistry) -> GuidelineContentV1:
        content = GuidelineContentV1.model_validate(self.model_dump())
        content.require_registered_parameters(registry)
        project_model_view(content)
        return content
