from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.source_record_reference import SourceRecordReference


class ContextPayload(DomainModel):
    payload_type: Literal["context"] = "context"
    summary_fields: tuple[tuple[NonEmptyString, NonEmptyString], ...]
    record_refs: tuple[SourceRecordReference, ...]

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if not self.summary_fields:
            raise ValueError("context payload requires summary_fields")
        names = tuple(name for name, _ in self.summary_fields)
        ensure_unique_tuple(names, field_name="context summary field names")
        ensure_unique_tuple(self.record_refs, field_name="context record refs")
        return self
