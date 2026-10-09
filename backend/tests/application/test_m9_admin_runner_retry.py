"""An accepted Admin operation survives temporary database failures in one process."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.settings_admin.admin_operation_runner import AdminOperationRunner
from app.domain.settings_admin import (
    AdminCommand, AdminOperation, AdminOperationState, AdminProgress,
    GenerationDeletionResult,
)
from app.ports.errors import PortUnavailableError


class _Clock:
    def now(self):
        return datetime.now(UTC)


class _Store:
    def __init__(self, operation, fail_state, commit_before_failure):
        self.operation = operation
        self.fail_state = fail_state
        self.commit_before_failure = commit_before_failure
        self.failed = False
        self.read_failed = False

    async def get(self, _operation_id):
        if self.failed and not self.read_failed and not self.commit_before_failure:
            self.read_failed = True
            raise PortUnavailableError("database unavailable")
        return self.operation

    async def compare_and_swap_state(self, operation, *, expected):
        assert self.operation.state is expected
        if operation.state is self.fail_state and not self.failed:
            self.failed = True
            if self.commit_before_failure:
                self.operation = operation
            raise PortUnavailableError("database unavailable")
        self.operation = operation
        return operation


@pytest.mark.parametrize("fail_state", [AdminOperationState.RUNNING, AdminOperationState.SUCCEEDED])
@pytest.mark.parametrize("commit_before_failure", [False, True])
def test_temporary_or_ambiguous_database_failure_is_recovered_without_reexecution(
    fail_state, commit_before_failure,
) -> None:
    async def exercise():
        generation_id = str(uuid4())
        command = AdminCommand(
            kind="generation_deletion", resource_identity=f"rag:{generation_id}",
            facts_digest="a" * 64, generation_id=generation_id,
        )
        operation = AdminOperation(
            operation_id=uuid4(), kind="generation_deletion", state="pending",
            resource_identity=command.resource_identity,
            progress=AdminProgress(total_items=1), created_at=datetime.now(UTC),
        )
        store = _Store(operation, fail_state, commit_before_failure)
        calls = 0

        async def handle(_command):
            nonlocal calls
            calls += 1
            return GenerationDeletionResult(deleted_generation_id=generation_id)

        runner = AdminOperationRunner(store=store, clock=_Clock(), handle=handle)
        runner.schedule(operation, command)
        for _ in range(100):
            if store.operation.state is AdminOperationState.SUCCEEDED and not runner._tasks:
                break
            await asyncio.sleep(0.01)
        assert store.failed
        assert store.operation.state is AdminOperationState.SUCCEEDED
        assert store.operation.safe_result.deleted_generation_id == generation_id
        assert calls == 1
        await runner.shutdown()

    asyncio.run(exercise())
