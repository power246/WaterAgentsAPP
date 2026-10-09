"""M9 domain contract."""

from typing import Self
from pydantic import model_validator
from app.domain.common import DomainModel
from .count import Count


class AdminProgress(DomainModel):
    processed_items: Count = 0
    total_items: Count | None = None
    succeeded_items: Count = 0
    failed_items: Count = 0

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.succeeded_items + self.failed_items > self.processed_items:
            raise ValueError("finished items cannot exceed processed items")
        if self.total_items is not None and self.processed_items > self.total_items:
            raise ValueError("processed items cannot exceed total items")
        return self
