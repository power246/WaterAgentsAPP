from typing import Annotated

from pydantic import Field

from app.domain.common import DomainModel, JsonValue, NonEmptyString
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.evidence_location import EvidenceLocation
from app.domain.evidence.provenance import Provenance
from app.domain.hydrology import ParameterKey
from app.domain.time import TimeRange


class EvidenceFragment(DomainModel):
    evidence_id: NonEmptyString
    source: NonEmptyString
    collection_status: CollectionStatus
    target_comid: NonEmptyString | None
    location: EvidenceLocation
    time_range: TimeRange
    parameter_refs: tuple[ParameterKey, ...] = ()
    data_type: NonEmptyString
    value_or_summary: JsonValue
    quality_flags: tuple[DataQualityFlag, ...] = ()
    confidence_hint: Annotated[float, Field(ge=0, le=1)] | None
    provenance: Provenance
    rag_eligible: bool = False
