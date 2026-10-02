from typing import Literal

from pydantic import field_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_tool_options import EvidenceToolOptions
from app.domain.hydrology.parameter_key import ParameterKey


class EvidenceToolRequest(DomainModel):
    schema_version: Literal[1] = 1
    tool_call_id: NonEmptyString
    session_id: NonEmptyString
    tool_name: NonEmptyString
    purpose: NonEmptyString
    window_ids: tuple[NonEmptyString, ...]
    parameter_keys: tuple[ParameterKey, ...]
    options: EvidenceToolOptions = EvidenceToolOptions()

    @field_validator("window_ids", "parameter_keys")
    @classmethod
    def validate_nonempty_unique_values(
        cls,
        value: tuple[object, ...],
    ) -> tuple[object, ...]:
        if not value:
            raise ValueError("tool request windows and parameters must not be empty")
        return ensure_unique_tuple(value, field_name="tool request values")
