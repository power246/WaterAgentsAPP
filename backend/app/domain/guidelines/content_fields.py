from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.domain.domain_model import DomainModel
from app.domain.guidelines.canonical import NormalizedText, digest, unique_parameters, unique_texts
from app.domain.guidelines.source_ref import GuidelineSourceRef
from app.domain.guidelines.source_kind import GuidelineSourceKind
from app.domain.hydrology.parameter_key import ParameterKey


class _GuidelineContentFields(DomainModel):
    schema_version: Literal[1] = 1
    title: Annotated[NormalizedText, Field(max_length=160)] | None = None
    summary: Annotated[NormalizedText, Field(max_length=240)] | None = None
    parameter_keys: tuple[ParameterKey, ...] = ()
    related_parameter_keys: tuple[ParameterKey, ...] = ()
    scenario_tags: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=48)], ...],
        Field(max_length=12),
    ] = ()
    keywords: Annotated[
        tuple[Annotated[NormalizedText, Field(min_length=1, max_length=64)], ...],
        Field(max_length=24),
    ] = ()
    language: Literal["zh", "en", "mixed"] | None = None
    source_kind: GuidelineSourceKind | None = None
    body_markdown: NormalizedText | None = None
    sources: Annotated[tuple[GuidelineSourceRef, ...], Field(max_length=16)] = ()

    @field_validator("parameter_keys", "related_parameter_keys")
    @classmethod
    def canonical_parameters(cls, value):
        return unique_parameters(value)

    @field_validator("scenario_tags", "keywords", mode="before")
    @classmethod
    def canonical_metadata(cls, value):
        return unique_texts(value)

    @field_validator("scenario_tags", "keywords")
    @classmethod
    def nonblank_metadata(cls, value):
        if any(not item.strip() for item in value):
            raise ValueError("metadata items must not be blank")
        return value

    @field_validator("sources")
    @classmethod
    def canonical_sources(cls, value):
        by_id = {}
        for source in value:
            if source.source_id in by_id and by_id[source.source_id] != source:
                raise ValueError("source ID has conflicting metadata")
            by_id[source.source_id] = source
        return tuple(by_id[key] for key in sorted(by_id))

    @property
    def content_digest(self) -> str:
        return digest(self.model_dump(mode="json"))
