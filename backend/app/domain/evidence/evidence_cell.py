from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_cell_role import EvidenceCellRole
from app.domain.evidence.evidence_cell_status import EvidenceCellStatus
from app.domain.hydrology.parameter_key import ParameterKey


class EvidenceCell(DomainModel):
    parameter: ParameterKey
    window_id: NonEmptyString
    role: EvidenceCellRole
    direct_fragment_refs: tuple[NonEmptyString, ...] = ()
    allowed_indirect_fragment_refs: tuple[NonEmptyString, ...] = ()
    contextual_fragment_refs: tuple[NonEmptyString, ...] = ()
    collection_fact_refs: tuple[NonEmptyString, ...] = ()
    status: EvidenceCellStatus
    reasons: tuple[NonEmptyString, ...] = ()

    @field_validator(
        "direct_fragment_refs",
        "allowed_indirect_fragment_refs",
        "contextual_fragment_refs",
        "collection_fact_refs",
        "reasons",
    )
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="cell references or reasons")

    @model_validator(mode="after")
    def validate_status_and_references(self) -> Self:
        reference_groups = (
            self.direct_fragment_refs,
            self.allowed_indirect_fragment_refs,
            self.contextual_fragment_refs,
            self.collection_fact_refs,
        )
        all_references = tuple(
            reference for group in reference_groups for reference in group
        )
        ensure_unique_tuple(all_references, field_name="cell fragment references")
        usable = self.direct_fragment_refs + self.allowed_indirect_fragment_refs
        if self.status is EvidenceCellStatus.SUPPORTED and not usable:
            raise ValueError("supported cell requires direct or allowed-indirect evidence")
        if self.status is EvidenceCellStatus.UNSUPPORTED and usable:
            raise ValueError("unsupported cell must not contain usable evidence")
        if self.status is not EvidenceCellStatus.SUPPORTED and not self.reasons:
            raise ValueError("unsupported or blocked cell requires reasons")
        return self

    def fragment_ids(self) -> tuple[str, ...]:
        return (
            self.direct_fragment_refs
            + self.allowed_indirect_fragment_refs
            + self.contextual_fragment_refs
            + self.collection_fact_refs
        )
