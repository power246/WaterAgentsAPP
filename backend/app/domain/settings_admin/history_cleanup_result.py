"""M9 domain contract."""

from typing import Literal
from uuid import UUID
from app.domain.common import DomainModel
from .count import Count
from .admin_action_kind import AdminActionKind


class HistoryCleanupResult(DomainModel):
    kind: Literal[AdminActionKind.HISTORY_CLEANUP] = AdminActionKind.HISTORY_CLEANUP
    deleted_run_ids: tuple[UUID, ...]
    retained_idempotency_tombstones: Count
