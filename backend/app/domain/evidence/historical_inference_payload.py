from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.item_reference import ItemReference


class HistoricalInferencePayload(DomainModel):
    payload_type: Literal["historical_inference"] = "historical_inference"
    original_item_ref: ItemReference
    projection: NonEmptyString
