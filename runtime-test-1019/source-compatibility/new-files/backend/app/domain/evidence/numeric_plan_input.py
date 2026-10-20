from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.hydrology.parameter_key import ParameterKey


class NumericPlanInput(DomainModel):
    parameter: ParameterKey
    unit: NonEmptyString
    window_id: NonEmptyString
    evidence_ref: EvidenceFragmentReference | None = None
    dependency_task_id: NonEmptyString | None = None
    required: StrictBool = True

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (self.evidence_ref is None) == (self.dependency_task_id is None):
            raise ValueError("numeric plan input needs one evidence or task reference")
        return self
