from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.common import NonEmptyString, ensure_unique_tuple
from app.domain.domain_model import DomainModel
from app.domain.guidelines.usage_target import GuidelineUsageTarget


class GuidelineResultUsageBinding(DomainModel):
    result_id: NonEmptyString
    target: GuidelineUsageTarget
    usage_item_ids: Annotated[tuple[UUID, ...], Field(min_length=1)]

    @field_validator("usage_item_ids")
    @classmethod
    def unique_usage_records(cls, value):
        return ensure_unique_tuple(value, field_name="result usage item IDs")
