from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel
from app.domain.inference.aggregation_method import AggregationMethod


class EvidenceToolOptions(DomainModel):
    record_limit: Annotated[int, Field(strict=True, ge=1, le=200)] | None = None
    page_offset: Annotated[int, Field(strict=True, ge=0)] | None = None
    aggregation_method: AggregationMethod | None = None
    include_forecast: bool | None = None
