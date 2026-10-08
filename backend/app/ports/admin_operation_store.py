from typing import Protocol
from datetime import datetime
from uuid import UUID

from app.domain.settings_admin import AdminOperation, AdminOperationState


class AdminOperationStorePort(Protocol):
    async def create(
        self, operation: AdminOperation, *, idempotency_key: UUID,
        request_fingerprint: bytes, confirmation_token_digest: bytes,
    ) -> AdminOperation: ...

    async def get(self, operation_id: UUID) -> AdminOperation | None: ...

    async def list_recent(
        self, *, limit: int, cursor: str | None,
    ) -> tuple[tuple[AdminOperation, ...], str | None]: ...

    async def compare_and_swap_state(
        self, operation: AdminOperation, *, expected: AdminOperationState,
    ) -> AdminOperation: ...

    async def reconcile_interrupted(self, *, now: datetime) -> int: ...
