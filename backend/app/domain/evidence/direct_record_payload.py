from typing import Literal

from app.domain.common import CanonicalDecimal, DomainModel, NonEmptyString
from app.domain.evidence.source_record_reference import SourceRecordReference


class DirectRecordPayload(DomainModel):
    payload_type: Literal["direct_record"] = "direct_record"
    record_ref: SourceRecordReference
    displayed_value: CanonicalDecimal
    displayed_unit: NonEmptyString
