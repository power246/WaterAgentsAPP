from typing import Literal

from pydantic import field_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime, ensure_unique_tuple
from app.domain.evidence.evidence_tool_options import EvidenceToolOptions
from app.domain.hydrology.parameter_key import ParameterKey


class EvidenceToolCall(DomainModel):
    schema_version: Literal[1] = 1
    tool_call_id: NonEmptyString
    session_id: NonEmptyString
    tool_name: NonEmptyString
    purpose: NonEmptyString
    window_ids: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    options: EvidenceToolOptions = EvidenceToolOptions()
    policy: Literal[
        "conditional_mandatory",
        "optional",
        "producer_slot",
        "deterministic",
    ]
    requested_at: UtcDatetime

    @field_validator("window_ids", "parameter_keys")
    @classmethod
    def validate_nonempty_unique_values(
        cls,
        value: tuple[object, ...],
    ) -> tuple[object, ...]:
        if not value:
            raise ValueError("tool call windows and parameters must not be empty")
        return ensure_unique_tuple(value, field_name="tool call values")

    def referenced_item_ids(self) -> tuple[str, ...]:
        item_ids: list[str] = []
        if self.options.source_item_id is not None:
            item_ids.append(self.options.source_item_id)
        item_ids.extend(item.item_id for item in self.options.source_record_refs)
        item_ids.extend(item.item_id for item in self.options.fragment_refs)
        return tuple(dict.fromkeys(item_ids))
