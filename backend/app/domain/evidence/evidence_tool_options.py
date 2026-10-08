from typing import Annotated

from pydantic import Field, field_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.hydrology.evidence_aggregation_method import (
    EvidenceAggregationMethod,
)
from app.domain.memory_rag import HistoricalSearchStrategy


class EvidenceToolOptions(DomainModel):
    record_limit: Annotated[int, Field(strict=True, ge=1, le=200)] | None = None
    page_offset: Annotated[int, Field(strict=True, ge=0)] | None = None
    aggregation_method: EvidenceAggregationMethod | None = None
    include_forecast: bool | None = None
    facility_radius_miles: Annotated[DomainFloat, Field(ge=1, le=20)] | None = None
    target_unit_id: NonEmptyString | None = None
    source_item_id: NonEmptyString | None = None
    source_record_refs: tuple[SourceRecordReference, ...] = ()
    fragment_refs: tuple[EvidenceFragmentReference, ...] = ()
    historical_search_strategy: HistoricalSearchStrategy | None = None

    @field_validator("source_record_refs", "fragment_refs")
    @classmethod
    def validate_unique_refs(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="evidence tool input refs")
