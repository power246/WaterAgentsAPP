from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import VersionNumber
from app.domain.guidelines.content import GuidelineContentV1
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.model_view import GuidelineModelViewV1, project_model_view
from app.domain.guidelines.review import GuidelineReview


class GuidelineVersionV1(DomainModel):
    schema_version: Literal[1] = 1
    version_id: UUID
    version_number: VersionNumber
    identity: GuidelineIdentity
    content: GuidelineContentV1
    model_view: GuidelineModelViewV1
    review: GuidelineReview

    @model_validator(mode="after")
    def bound_review_and_projection(self) -> Self:
        if (self.review.guideline_id != self.identity.guideline_id
                or self.review.content_digest != self.content.content_digest):
            raise ValueError("review must bind this guideline and exact content digest")
        if self.model_view != project_model_view(self.content):
            raise ValueError("model view must match the approved content projection")
        return self
