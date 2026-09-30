from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.collection_status import CollectionStatus


class WindowCompleteness(DomainModel):
    window_id: NonEmptyString
    complete: bool
    status: CollectionStatus
    reason: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if not self.complete and self.reason is None:
            raise ValueError("incomplete window requires reason")
        if self.status is CollectionStatus.EMPTY and not self.complete:
            raise ValueError("empty requires a completed provider query")
        return self
