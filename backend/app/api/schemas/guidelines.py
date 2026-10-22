from typing import Annotated
from uuid import UUID

from pydantic import Field, StrictBool, field_validator

from app.api.schemas.analysis_runs import AnalysisRunErrorResponse
from app.domain.domain_model import DomainModel
from app.domain.guidelines import GuidelineDocumentDetails, GuidelineDraftV1, GuidelineListItem
from app.domain.guidelines.canonical import Digest, NormalizedText, Revision


class SaveGuidelineDraftRequest(DomainModel):
    expected_revision: Revision
    draft: GuidelineDraftV1


class PublishGuidelineRequest(DomainModel):
    expected_revision: Revision
    content_digest: Digest
    review_confirmed: StrictBool
    reviewer: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    review_note: Annotated[NormalizedText, Field(min_length=1, max_length=1000)]

    @field_validator("reviewer", "review_note")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("review field must not be blank")
        return value


class LocalEditGuidelineRequest(DomainModel):
    expected_revision: Revision
    base_version_id: UUID
    base_content_digest: Digest


class SetGuidelineEnabledRequest(DomainModel):
    expected_revision: Revision
    enabled: StrictBool


class RestoreBundledGuidelineRequest(DomainModel):
    expected_revision: Revision
    version_id: UUID


class RetryGuidelineOperationRequest(DomainModel):
    expected_revision: Revision


class GuidelineListResponse(DomainModel):
    items: tuple[GuidelineListItem, ...]
    next_cursor: str | None


class GuidelineConflictResponse(AnalysisRunErrorResponse):
    current_document: GuidelineDocumentDetails | None = None
