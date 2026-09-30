from typing import Annotated, TypeAlias

from pydantic import Field

from app.domain.evidence.aggregate_payload import AggregatePayload
from app.domain.evidence.collection_fact_payload import CollectionFactPayload
from app.domain.evidence.context_payload import ContextPayload
from app.domain.evidence.conversion_payload import ConversionPayload
from app.domain.evidence.direct_record_payload import DirectRecordPayload
from app.domain.evidence.historical_inference_payload import (
    HistoricalInferencePayload,
)
from app.domain.evidence.upstream_result_payload import UpstreamResultPayload

EvidencePayload: TypeAlias = Annotated[
    DirectRecordPayload
    | AggregatePayload
    | ConversionPayload
    | ContextPayload
    | CollectionFactPayload
    | UpstreamResultPayload
    | HistoricalInferencePayload,
    Field(discriminator="payload_type"),
]
