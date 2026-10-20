from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.numeric_plan_input import NumericPlanInput
from app.domain.hydrology.parameter_key import ParameterKey


class NumericPlanTask(DomainModel):
    task_id: NonEmptyString
    target_comid: NonEmptyString
    output_parameter: ParameterKey
    output_unit: NonEmptyString
    window_id: NonEmptyString
    mode: NonEmptyString
    method_id: NonEmptyString
    method_version: Annotated[int, Field(strict=True, ge=1)]
    method_kind: Literal["explicit_calculation", "model_estimate"] = "explicit_calculation"
    station_id: NonEmptyString | None = None
    series_identity: NonEmptyString | None = None
    inputs: tuple[NumericPlanInput, ...]

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.inputs and self.method_kind != "model_estimate":
            raise ValueError("numeric plan task requires inputs")
        return self
