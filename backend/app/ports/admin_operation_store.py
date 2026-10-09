from typing import Protocol
from datetime import datetime
from uuid import UUID

from app.domain.settings_admin import AdminOperation, AdminOperationState
from app.domain.settings_admin import AdminCommand


class AdminOperationStorePort(Protocol):
    async def create(
        self, operation: AdminOperation, *, idempotency_key: UUID,
        request_fingerprint: bytes, confirmation_token_digest: bytes,
        command: AdminCommand | None = None,
    ) -> AdminOperation: ...

    async def read_receipt(
        self, idempotency_key: UUID,
    ) -> tuple[AdminOperation, bytes, AdminCommand | None] | None: ...

    async def list_pending(self) -> tuple[tuple[AdminOperation, AdminCommand | None], ...]: ...

    async def reconcile_running(self, *, now: datetime) -> int: ...

    async def get(self, operation_id: UUID) -> AdminOperation | None: ...

    async def list_recent(
        self, *, limit: int, cursor: str | None,
    ) -> tuple[tuple[AdminOperation, ...], str | None]: ...

    async def compare_and_swap_state(
        self, operation: AdminOperation, *, expected: AdminOperationState,
    ) -> AdminOperation: ...
