from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.item_reference import ItemReference


class UpstreamResultPayload(DomainModel):
    payload_type: Literal["upstream_result"] = "upstream_result"
    upstream_session_id: NonEmptyString
    result_ref: ItemReference
    closure_refs: tuple[ItemReference, ...]

    @model_validator(mode="after")
    def validate_closure(self) -> Self:
        if not self.closure_refs:
            raise ValueError("upstream result payload requires closure_refs")
        ensure_unique_tuple(self.closure_refs, field_name="upstream closure refs")
        return self
