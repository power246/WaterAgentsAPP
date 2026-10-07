from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.memory_rag.rag_cleanup_dependency import RAGCleanupDependency


class RAGCleanupPlan(DomainModel):
    requested_run_ids: tuple[NonEmptyString, ...]
    delete_run_ids: tuple[NonEmptyString, ...]
    delete_order: tuple[NonEmptyString, ...]
    dependencies: tuple[RAGCleanupDependency, ...] = ()
    cascade_required: StrictBool

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        ensure_unique_tuple(self.requested_run_ids, field_name="requested_run_ids")
        ensure_unique_tuple(self.delete_run_ids, field_name="delete_run_ids")
        ensure_unique_tuple(self.delete_order, field_name="delete_order")
        if set(self.delete_order) != set(self.delete_run_ids):
            raise ValueError("delete_order must contain every delete_run_id exactly once")
        if not set(self.requested_run_ids).issubset(self.delete_run_ids):
            raise ValueError("cleanup plan must retain every requested run")
        expected_cascade = bool(
            set(self.delete_run_ids).difference(self.requested_run_ids)
        )
        if self.cascade_required is not expected_cascade:
            raise ValueError("cascade_required must match dependent runs")
        return self
