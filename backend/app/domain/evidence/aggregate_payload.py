from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, DomainModel, NonEmptyString
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.inference.aggregation_method import AggregationMethod


class AggregatePayload(DomainModel):
    payload_type: Literal["aggregate"] = "aggregate"
    value: CanonicalDecimal
    unit: NonEmptyString
    method: AggregationMethod
    input_record_refs: tuple[SourceRecordReference, ...]
    valid_input_count: Annotated[int, Field(strict=True, ge=0)]
    suspicious_input_count: Annotated[int, Field(strict=True, ge=0)]
    invalid_input_count: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.input_record_refs:
            raise ValueError("aggregate requires input_record_refs")
        if len(self.input_record_refs) != len(set(self.input_record_refs)):
            raise ValueError("aggregate input_record_refs must be unique")
        if self.valid_input_count + self.suspicious_input_count == 0:
            raise ValueError("aggregate requires a valid or suspicious input")
        return self
