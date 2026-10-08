"""M9 domain contract."""

from typing import Self
from uuid import UUID
from pydantic import model_validator
from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from .admin_safe_result import AdminSafeResult
from .admin_action_kind import AdminActionKind
from .admin_operation_state import AdminOperationState
from .admin_progress import AdminProgress
from .admin_safe_error import AdminSafeError


class AdminOperation(DomainModel):
    operation_id: UUID
    kind: AdminActionKind
    state: AdminOperationState
    resource_identity: NonEmptyString
    progress: AdminProgress
    safe_result: AdminSafeResult | None = None
    safe_error: AdminSafeError | None = None
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.state is AdminOperationState.PENDING and self.started_at is not None:
            raise ValueError("pending operation cannot have started_at")
        if self.state not in {
            AdminOperationState.PENDING, AdminOperationState.INTERRUPTED,
        } and self.started_at is None:
            raise ValueError("started operation requires started_at")
        if self.state.terminal != (self.finished_at is not None):
            raise ValueError("terminal state must match finished_at")
        if self.state is AdminOperationState.SUCCEEDED:
            if self.safe_result is None or self.safe_error is not None:
                raise ValueError("successful operation requires only a safe result")
            if self.safe_result.kind is not self.kind:
                raise ValueError("result kind must match operation kind")
        elif self.safe_result is not None:
            raise ValueError("only successful operation may have a result")
        if self.state in {AdminOperationState.FAILED, AdminOperationState.INTERRUPTED}:
            if self.safe_error is None:
                raise ValueError("failed operation requires safe_error")
        elif self.safe_error is not None:
            raise ValueError("active/successful operation cannot have safe_error")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at precedes created_at")
        if self.finished_at is not None and self.finished_at < (
            self.started_at or self.created_at
        ):
            raise ValueError("finished_at precedes started_at")
        return self

    def transition(self, next_snapshot: Self) -> Self:
        next_snapshot = type(self).model_validate(next_snapshot.model_dump())
        allowed = {
            AdminOperationState.PENDING: {
                AdminOperationState.RUNNING, AdminOperationState.INTERRUPTED,
            },
            AdminOperationState.RUNNING: {
                AdminOperationState.RUNNING,
                AdminOperationState.SUCCEEDED, AdminOperationState.FAILED,
                AdminOperationState.INTERRUPTED,
            },
        }
        if next_snapshot.state not in allowed.get(self.state, set()):
            raise ValueError("admin operation transition is not allowed")
        if (
            next_snapshot.operation_id != self.operation_id
            or next_snapshot.kind is not self.kind
            or next_snapshot.resource_identity != self.resource_identity
            or next_snapshot.created_at != self.created_at
        ):
            raise ValueError("admin operation identity cannot change")
        if any(
            getattr(next_snapshot.progress, field) < getattr(self.progress, field)
            for field in ("processed_items", "succeeded_items", "failed_items")
        ):
            raise ValueError("admin operation progress cannot decrease")
        return next_snapshot
