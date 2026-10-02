from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, NonEmptyString
from app.domain.evidence.item_reference import ItemReference
from app.domain.evidence.source_record_base import _SourceRecordBase
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.result_source import ResultSource


class UpstreamResultRecord(_SourceRecordBase):
    record_type: Literal["upstream_result"] = "upstream_result"
    upstream_session_id: NonEmptyString
    result_ref: ItemReference
    parameter: ParameterKey
    window_id: NonEmptyString
    result_kind: NonEmptyString
    result_source: ResultSource
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    evidence_closure_refs: tuple[ItemReference, ...]

    @model_validator(mode="after")
    def validate_upstream_result(self) -> Self:
        if self.valid_time_range is None:
            raise ValueError("upstream result requires valid_time_range")
        if self.raw_unit is not None:
            raise ValueError("upstream result must not declare raw_unit")
        if not self.evidence_closure_refs:
            raise ValueError("upstream result requires evidence closure refs")
        item_ids = tuple(reference.item_id for reference in self.evidence_closure_refs)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("evidence closure refs must not contain duplicates")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        return (
            self.result_ref.item_id,
            *(reference.item_id for reference in self.evidence_closure_refs),
        )
