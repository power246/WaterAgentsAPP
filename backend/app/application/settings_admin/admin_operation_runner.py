"""Single-process executor for committed, durable Admin operation rows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from app.domain.settings_admin import AdminCommand
from app.application.settings_admin.errors import (
    AdminOperationConflictError, SettingsAdminError,
)
from app.application.transcript_memory.maintenance import RAGMaintenanceConflictError
from app.domain.settings_admin import (
    AdminOperation, AdminOperationState, AdminProgress, AdminSafeError,
)
from app.ports.admin_operation_store import AdminOperationStorePort
from app.ports.clock import ClockPort
from app.ports.errors import PortUnavailableError


class AdminOperationRunner:
    def __init__(
        self, *, store: AdminOperationStorePort, clock: ClockPort,
        handle: Callable[[AdminCommand], Awaitable[object]],
    ) -> None:
        self._store = store
        self._clock = clock
        self._handle = handle
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._stopping = False

    async def start(self) -> None:
        await self._store.reconcile_running(now=self._clock.now())
        for operation, command in await self._store.list_pending():
            if command is None:
                await self._store.compare_and_swap_state(
                    _next(operation, AdminOperationState.INTERRUPTED,
                          now=self._clock.now(), safe_error=AdminSafeError(
                              code="operation_interrupted",
                              message="The operation cannot be resumed.", retryable=True,
                          )),
                    expected=AdminOperationState.PENDING,
                )
            else:
                self.schedule(operation, command)

    def schedule(self, operation: AdminOperation, command: AdminCommand) -> None:
        if self._stopping:
            return
        if operation.state is not AdminOperationState.PENDING:
            return
        task = self._tasks.get(operation.operation_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._run_one(operation, command),
            name=f"admin-operation-{operation.operation_id}",
        )
        self._tasks[operation.operation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(operation.operation_id, None))

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_one(self, operation: AdminOperation, command: AdminCommand) -> None:
        running = await self._persist_state(
            _next(operation, AdminOperationState.RUNNING, now=self._clock.now()),
            expected=AdminOperationState.PENDING,
        )
        if running is None:
            return
        try:
            result = await self._handle(command)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._persist_state(
                _next(running, AdminOperationState.FAILED, now=self._clock.now(),
                      safe_error=_safe_error(error)),
                expected=AdminOperationState.RUNNING,
            )
            return
        total = running.progress.total_items or 1
        if command.kind.value == "retry_failed_jobs":
            total = result.retried_jobs
        await self._persist_state(
            _next(running, AdminOperationState.SUCCEEDED, now=self._clock.now(),
                  safe_result=result, processed=total),
            expected=AdminOperationState.RUNNING,
        )

    async def _persist_state(
        self, desired: AdminOperation, *, expected: AdminOperationState,
    ) -> AdminOperation | None:
        delay = 0.1
        uncertain = False
        while True:
            try:
                return await self._store.compare_and_swap_state(desired, expected=expected)
            except AdminOperationConflictError:
                if not uncertain:
                    return None
            except PortUnavailableError:
                uncertain = True
            try:
                current = await self._store.get(desired.operation_id)
            except PortUnavailableError:
                current = None
            if current is not None:
                if current.state is desired.state:
                    return current
                if current.state is not expected:
                    return None
                uncertain = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


def _next(
    current: AdminOperation, state: AdminOperationState, *, now: datetime,
    safe_result: object | None = None, safe_error: AdminSafeError | None = None,
    processed: int = 0,
) -> AdminOperation:
    data = current.model_dump(mode="python")
    data.update(state=state, safe_result=safe_result, safe_error=safe_error)
    if state is AdminOperationState.RUNNING:
        data["started_at"] = now
    if state.terminal:
        data["finished_at"] = now
    if state is AdminOperationState.SUCCEEDED:
        data["progress"] = AdminProgress(
            total_items=current.progress.total_items,
            processed_items=processed, succeeded_items=processed,
        )
    return AdminOperation.model_validate(data)


def _safe_error(error: Exception) -> AdminSafeError:
    if isinstance(error, SettingsAdminError):
        return AdminSafeError(
            code=error.code, message=error.public_message, retryable=error.retryable,
        )
    if isinstance(error, (PortUnavailableError, TimeoutError)):
        return AdminSafeError(
            code="operation_dependency_unavailable",
            message="The operation dependency is unavailable.", retryable=True,
        )
    if isinstance(error, RAGMaintenanceConflictError):
        return AdminSafeError(
            code="rag_generation_conflict",
            message="The RAG generation changed or is not eligible.", retryable=False,
        )
    return AdminSafeError(
        code="operation_failed", message="The operation could not be completed.",
        retryable=False,
    )
