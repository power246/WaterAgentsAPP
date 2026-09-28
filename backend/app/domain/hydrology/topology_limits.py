from pydantic import Field

from app.domain.common import DomainFloat, DomainModel


class TopologyLimits(DomainModel):
    max_distance_km: DomainFloat = Field(gt=0)
    max_hops: int = Field(strict=True, ge=1)
    max_nodes: int = Field(strict=True, ge=1)
