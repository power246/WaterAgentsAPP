from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, ensure_unique_tuple
from app.domain.hydrology.evidence_rule_id import EvidenceRuleId
from app.domain.hydrology.parameter_key import ParameterKey


class EvidenceRelationDefinition(DomainModel):
    relation_id: EvidenceRuleId
    same_parameter: StrictBool
    input_parameter_keys: tuple[ParameterKey, ...]
    output_parameter_keys: tuple[ParameterKey, ...]

    @model_validator(mode="after")
    def validate_relation(self) -> Self:
        ensure_unique_tuple(
            self.input_parameter_keys,
            field_name="input_parameter_keys",
        )
        ensure_unique_tuple(
            self.output_parameter_keys,
            field_name="output_parameter_keys",
        )
        if self.same_parameter:
            if self.input_parameter_keys or self.output_parameter_keys:
                raise ValueError("same-parameter relation must not fix parameter keys")
        elif not self.input_parameter_keys or not self.output_parameter_keys:
            raise ValueError("fixed relation requires input and output parameter keys")
        return self
