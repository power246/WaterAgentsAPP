from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, DomainModel, NonEmptyString
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.item_reference import ItemReference


class TemporalEstimatePayload(DomainModel):
    payload_type: Literal["temporal_estimate"] = "temporal_estimate"
    value: CanonicalDecimal
    unit: NonEmptyString
    method: Literal["linear_interpolation", "nearest_hold"]
    input_evidence_refs: tuple[EvidenceFragmentReference, ...]
    anchor_window_ids: tuple[NonEmptyString, ...]
    anchor_distance_seconds: tuple[Annotated[int, Field(strict=True, gt=0)], ...]
    plan_item_ref: ItemReference | None = None
    task_id: NonEmptyString | None = None
    plan_digest: NonEmptyString | None = None
    method_registry_digest: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_anchors(self) -> Self:
        expected = 2 if self.method == "linear_interpolation" else 1
        if len(self.input_evidence_refs) != expected:
            raise ValueError("temporal estimate method requires matching anchor count")
        if len(set(self.input_evidence_refs)) != expected:
            raise ValueError("temporal estimate anchors must be unique")
        if (
            len(self.anchor_window_ids) != expected
            or len(set(self.anchor_window_ids)) != expected
        ):
            raise ValueError("temporal estimate window IDs must match anchors")
        if len(self.anchor_distance_seconds) != expected:
            raise ValueError("temporal estimate distances must match anchors")
        planning = (
            self.plan_item_ref,
            self.task_id,
            self.plan_digest,
            self.method_registry_digest,
        )
        if any(value is None for value in planning) and any(
            value is not None for value in planning
        ):
            raise ValueError("temporal estimate planning fields must be complete")
        return self
