from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.memory_rag.historical_topology_relation import (
    HistoricalTopologyRelation,
)


class AuthorizedRAGComid(DomainModel):
    """One COMID admitted by the saved current-run graph and its exact signals."""

    comid: NonEmptyString
    spatial_distance_km: Annotated[DomainFloat, Field(ge=0)] | None = None
    topology_relation: HistoricalTopologyRelation | None = None
    topology_hops: Annotated[int, Field(strict=True, ge=1)] | None = None
    topology_path_distance_km: Annotated[DomainFloat, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_topology_tuple(self) -> Self:
        topology = (
            self.topology_relation,
            self.topology_hops,
            self.topology_path_distance_km,
        )
        if any(value is not None for value in topology) and not all(
            value is not None for value in topology
        ):
            raise ValueError("authorized topology fields must be supplied together")
        return self
