from typing import Self

from pydantic import model_validator

from app.domain.common import (
    DomainModel,
    JsonValue,
    NonEmptyString,
    ensure_unique_tuple,
)


class AnalysisStep(DomainModel):
    type: NonEmptyString
    title: NonEmptyString
    content: JsonValue
    evidence_refs: tuple[NonEmptyString, ...] = ()
    transcript_item_refs: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        ensure_unique_tuple(self.evidence_refs, field_name="evidence_refs")
        ensure_unique_tuple(
            self.transcript_item_refs,
            field_name="transcript_item_refs",
        )
        return self
