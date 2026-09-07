from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.parameter_key import ParameterKey


class WaterQualityParameter(DomainModel):
    key: ParameterKey
    name: NonEmptyString
    canonical_unit: NonEmptyString | None
    is_dimensionless: bool = False
    category: NonEmptyString | None

    @model_validator(mode="after")
    def validate_canonical_unit(self) -> Self:
        if not self.is_dimensionless and self.canonical_unit is None:
            raise ValueError("canonical_unit is required for dimensional parameters")
        return self
