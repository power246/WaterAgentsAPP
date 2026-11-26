from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, digest, require_safe_model_text


class GuidelineEmbeddingProfileV1(DomainModel):
    namespace: Literal["guideline"] = "guideline"
    provider: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    model: Annotated[NormalizedText, Field(min_length=1, max_length=240)]
    output_dimensions: Annotated[int, Field(strict=True, ge=1)]
    max_input_tokens: Annotated[int, Field(strict=True, ge=1)]
    distance_metric: Literal["cosine"] = "cosine"
    text_contract_version: Literal[1, 2, 3] = 3
    document_template_version: Literal[1, 2, 3] = 3
    query_template_version: Literal[1] = 1
    document_instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)]
    query_instructions: Annotated[NormalizedText, Field(min_length=1, max_length=2000)]
    profile_id: str = "__auto__"
    profile_digest: str = "__auto__"

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.text_contract_version != self.document_template_version:
            raise ValueError("guideline text and document template versions must agree")
        for field in ("provider", "model", "document_instructions", "query_instructions"):
            text = getattr(self, field)
            if not text.strip():
                raise ValueError(f"{field} must not be blank")
            require_safe_model_text(text)
        expected_digest = digest(self.model_dump(mode="json", exclude={"profile_id", "profile_digest"}))
        for field, expected in (("profile_digest", expected_digest), ("profile_id", "guideline:" + expected_digest)):
            if getattr(self, field) not in {"__auto__", expected}:
                raise ValueError(f"{field} does not match the guideline profile")
            object.__setattr__(self, field, expected)
        return self
