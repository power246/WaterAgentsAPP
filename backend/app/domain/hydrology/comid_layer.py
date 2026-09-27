from typing import Self

from pydantic import RootModel, model_validator

from app.domain.common import NonEmptyString, ensure_unique_tuple
from app.domain.hydrology._topology_validation import validate_comid


class ComidLayer(RootModel[tuple[NonEmptyString, ...]]):
    @model_validator(mode="after")
    def validate_layer(self) -> Self:
        if not self.root:
            raise ValueError("execution layer must not be empty")
        for comid in self.root:
            validate_comid(comid)
        ensure_unique_tuple(self.root, field_name="execution layer")
        return self
