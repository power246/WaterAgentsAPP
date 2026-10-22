from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.common import UtcDatetime
from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import Digest, NormalizedText


class GuidelineReview(DomainModel):
    guideline_id: UUID
    content_digest: Digest
    review_confirmed: Literal[True]
    reviewer: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    review_note: Annotated[NormalizedText, Field(min_length=1, max_length=1000)]
    reviewed_at: UtcDatetime

    @field_validator("review_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("review confirmation must be explicit true")
        return value

    @field_validator("reviewer", "review_note")
    @classmethod
    def nonblank_review(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review fields must not be blank")
        return value
