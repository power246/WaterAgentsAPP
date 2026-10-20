from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import (
    CanonicalDecimal,
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.item_reference import ItemReference
from app.domain.evidence.measured_anchor import MeasuredAnchor


class NumericDerivedPayload(DomainModel):
    payload_type: Literal["numeric_derived"] = "numeric_derived"
    value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    method_kind: Literal["explicit_calculation", "model_estimate"] = "explicit_calculation"
    inference_basis: Literal["same_parameter_measurement", "related_evidence", "general_knowledge"] | None = None
    measured_anchors: tuple[MeasuredAnchor, ...] = ()
    rationale: str | None = None
    model_provider: NonEmptyString | None = None
    model_name: NonEmptyString | None = None
    model_operation_id: NonEmptyString | None = None
    prompt_digest: NonEmptyString | None = None
    unit: NonEmptyString
    method_id: NonEmptyString
    method_version: Annotated[int, Field(strict=True, ge=1)]
    task_id: NonEmptyString
    plan_digest: NonEmptyString
    plan_item_ref: ItemReference
    method_registry_digest: NonEmptyString
    input_evidence_refs: tuple[EvidenceFragmentReference, ...]
    input_window_ids: tuple[NonEmptyString, ...]
    confidence_cap: Annotated[DomainFloat, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.input_evidence_refs and self.method_kind != "model_estimate":
            raise ValueError("numeric derivation requires saved input evidence")
        ensure_unique_tuple(
            self.input_evidence_refs, field_name="numeric derivation input evidence"
        )
        if len(self.input_window_ids) != len(self.input_evidence_refs):
            raise ValueError("numeric derivation input windows must match references")
        if self.method_kind == "explicit_calculation":
            if self.value is None:
                raise ValueError("explicit calculation requires a value")
            if any(value is not None for value in (
                self.value_lower, self.value_upper, self.inference_basis,
                self.rationale, self.model_provider, self.model_name,
                self.model_operation_id, self.prompt_digest,
            )):
                raise ValueError("explicit calculation must not claim model estimation")
        else:
            from decimal import Decimal

            if any(value is None for value in (
                self.value_lower, self.value_upper, self.inference_basis,
                self.rationale, self.model_provider, self.model_name,
                self.model_operation_id, self.prompt_digest,
            )) or not self.rationale.strip():
                raise ValueError("model estimation requires bounds and model provenance")
            lower, upper = Decimal(self.value_lower), Decimal(self.value_upper)
            if lower >= upper:
                raise ValueError("estimate range must have ordered distinct bounds")
            if self.value is not None and not lower <= Decimal(self.value) <= upper:
                raise ValueError("estimate value must lie within its range")
            anchored = bool(self.measured_anchors)
            if anchored != (self.inference_basis == "same_parameter_measurement"):
                raise ValueError("estimate basis must match original measured anchors")
            if self.value is not None and not anchored:
                raise ValueError("point estimate requires an original same-parameter measurement")
        return self
