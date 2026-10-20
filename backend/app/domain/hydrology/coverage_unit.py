from pydantic import Field

from app.domain.common import DomainModel
from app.domain.hydrology.parameter_key import ParameterKey


class CoverageUnit(DomainModel):
    parameter: ParameterKey
    segment_index: int = Field(strict=True, ge=0)
    window_index: int | None = Field(default=None, strict=True, ge=0)
