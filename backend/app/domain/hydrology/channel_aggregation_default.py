from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.evidence_aggregation_method import (
    EvidenceAggregationMethod,
)
from app.domain.hydrology.parameter_provider import ParameterProvider


class ChannelAggregationDefault(DomainModel):
    provider: ParameterProvider
    channel: NonEmptyString
    resolution: Literal["6h", "12h", "24h"]
    method: EvidenceAggregationMethod
