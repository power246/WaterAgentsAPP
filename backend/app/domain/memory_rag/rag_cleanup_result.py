from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class RAGCleanupResult(DomainModel):
    requested_run_ids: tuple[NonEmptyString, ...]
    deleted_run_ids: tuple[NonEmptyString, ...]
    delete_order: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        ensure_unique_tuple(self.requested_run_ids, field_name="requested_run_ids")
        ensure_unique_tuple(self.deleted_run_ids, field_name="deleted_run_ids")
        ensure_unique_tuple(self.delete_order, field_name="delete_order")
        if set(self.delete_order) != set(self.deleted_run_ids):
            raise ValueError("delete_order must contain every deleted run exactly once")
        return self
