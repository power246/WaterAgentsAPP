from pydantic import Field

from app.domain.common import DomainFloat, DomainModel


class TopologySearchStats(DomainModel):
    discovered_nodes: int = Field(strict=True, ge=1)
    queried_nodes: int = Field(strict=True, ge=1)
    max_depth: int = Field(strict=True, ge=0)
    max_distance_km: DomainFloat = Field(ge=0)
    qualified_inlets: int = Field(strict=True, ge=0)
    excluded_nodes: int = Field(strict=True, ge=0)
