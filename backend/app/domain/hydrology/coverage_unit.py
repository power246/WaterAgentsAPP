from pydantic import Field

from app.domain.common import DomainModel
from app.domain.hydrology.parameter_key import ParameterKey


class CoverageUnit(DomainModel):
    parameter: ParameterKey
    segment_index: int = Field(strict=True, ge=0)
