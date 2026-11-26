from datetime import date
from typing import Annotated

from pydantic import Field, field_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, SourceId
from app.domain.guidelines.source_ref import GuidelineSourceRef


class GuidelineDraftSourceRef(DomainModel):
    source_id: SourceId
    title: Annotated[NormalizedText, Field(max_length=240)] | None = None
    url: Annotated[str, Field(max_length=2048)] | None = None
    locator: Annotated[NormalizedText, Field(max_length=240)] | None = None
    published_date: date | None = None
    accessed_date: date | None = None

    @field_validator("url", mode="before")
    @classmethod
    def safe_optional_url(cls, value):
        return GuidelineSourceRef.safe_http_url(None if value == "" else value)
