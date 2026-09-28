from typing import Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.hydrology._topology_validation import validate_comid


class HydrologyEdge(DomainModel):
    upstream_comid: NonEmptyString
    downstream_comid: NonEmptyString
    distance_km: DomainFloat = Field(gt=0)
    participating: bool

    @field_validator("upstream_comid", "downstream_comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        return validate_comid(value)

    @model_validator(mode="after")
    def reject_self_edge(self) -> Self:
        if self.upstream_comid == self.downstream_comid:
            raise ValueError("hydrology edges must not be self-referential")
        return self
