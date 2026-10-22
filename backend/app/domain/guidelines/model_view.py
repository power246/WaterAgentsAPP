import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import (
    Digest, NormalizedText, SourceId, canonical_text, digest, require_safe_model_text,
)
from app.domain.guidelines.content import GuidelineContentV1
from app.domain.guidelines.source_kind import GuidelineSourceKind
from app.domain.guidelines.validation_error import GuidelineValidationError

MAX_MODEL_VIEW_BYTES = 12_000
_URL = re.compile(r"https?://[^\s<>\[\]\"'`，。；！？、：）】》」』]+", re.IGNORECASE)


class GuidelineModelViewV1(DomainModel):
    schema_version: Literal[1] = 1
    title: Annotated[NormalizedText, Field(min_length=1, max_length=160)]
    summary: Annotated[NormalizedText, Field(min_length=1, max_length=240)]
    source_kind: GuidelineSourceKind
    body: Annotated[NormalizedText, Field(min_length=1)]
    source_ids: Annotated[tuple[SourceId, ...], Field(max_length=16)]
    content_digest: Digest

    @model_validator(mode="after")
    def safe_bounded_view(self) -> Self:
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("model-view source IDs must be unique and sorted")
        for field in ("title", "summary", "body"):
            try:
                require_safe_model_text(getattr(self, field))
            except ValueError:
                raise GuidelineValidationError(field, "unsafe_model_text") from None
        markers = set(re.findall(r"\[source:([^\]]+)\]", self.body))
        if not markers <= set(self.source_ids):
            raise ValueError("model-view source marker is not registered")
        if len(canonical_text(self.model_dump(mode="json")).encode("utf-8")) > MAX_MODEL_VIEW_BYTES:
            raise GuidelineValidationError("body", "model_view_too_large")
        return self

    @property
    def view_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


def project_model_view(content: GuidelineContentV1) -> GuidelineModelViewV1:
    by_url = {}
    for source in content.sources:
        if source.url is not None:
            by_url.setdefault(source.url, source.source_id)
    urls = sorted(by_url, key=lambda url: (-len(url), url))

    def replace_url(match: re.Match) -> str:
        original = match.group()
        for url in urls:
            if original.startswith(url):
                suffix = original[len(url):]
                if all(character in ").,;:!?}" for character in suffix):
                    return f"[source:{by_url[url]}]{suffix}"
        raise GuidelineValidationError("body_markdown", "source_url_not_registered")

    body = _URL.sub(replace_url, content.body_markdown)
    try:
        return GuidelineModelViewV1(
            title=content.title, summary=content.summary, source_kind=content.source_kind,
            body=body, source_ids=tuple(source.source_id for source in content.sources),
            content_digest=content.content_digest,
        )
    except ValueError as error:
        if isinstance(error, GuidelineValidationError):
            raise
        # Pydantic wraps model-validator errors. Keep the publication error safe and field-specific.
        for field in ("title", "summary", "body_markdown"):
            try:
                require_safe_model_text(body if field == "body_markdown" else getattr(content, field))
            except ValueError:
                raise GuidelineValidationError(field, "unsafe_model_text") from None
        raise GuidelineValidationError("body_markdown", "model_view_invalid") from None
