"""An accepted Admin operation survives temporary database failures in one process."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.settings_admin.admin_operation_runner import AdminOperationRunner
from app.application.settings_admin.errors import AdminOperationConflictError
from app.domain.settings_admin import (
    AdminCommand, AdminOperation, AdminOperationState, AdminProgress,
    GenerationDeletionResult,
)
from app.ports.errors import PortUnavailableError


class _Clock:
    def now(self):
        return datetime.now(UTC)


class _Store:
    def __init__(self, operation, fail_state, commit_before_failure, read_fail_after_commit=False):
        self.operation = operation
        self.fail_state = fail_state
        self.commit_before_failure = commit_before_failure
        self.read_fail_after_commit = read_fail_after_commit
        self.failed = False
        self.read_failed = False

    async def get(self, _operation_id):
        if self.failed and not self.read_failed and (not self.commit_before_failure or self.read_fail_after_commit):
            self.read_failed = True
            raise PortUnavailableError("database unavailable")
        return self.operation

    async def compare_and_swap_state(self, operation, *, expected):
        if self.operation.state is not expected:
            raise AdminOperationConflictError()
        if operation.state is self.fail_state and not self.failed:
            self.failed = True
            if self.commit_before_failure:
                self.operation = operation
            raise PortUnavailableError("database unavailable")
        self.operation = operation
        return operation


@pytest.mark.parametrize("fail_state,commit_before_failure,read_fail_after_commit", [
    (AdminOperationState.RUNNING, False, False),
    (AdminOperationState.RUNNING, True, False),
    (AdminOperationState.RUNNING, True, True),
    (AdminOperationState.SUCCEEDED, False, False),
    (AdminOperationState.SUCCEEDED, True, False),
    (AdminOperationState.SUCCEEDED, True, True),
])
def test_temporary_or_ambiguous_database_failure_is_recovered_without_reexecution(
    fail_state, commit_before_failure, read_fail_after_commit,
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
        store = _Store(operation, fail_state, commit_before_failure, read_fail_after_commit)
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
        if read_fail_after_commit:
            assert store.read_failed
        assert store.operation.state is AdminOperationState.SUCCEEDED
        assert store.operation.safe_result.deleted_generation_id == generation_id
        assert calls == 1
        await runner.shutdown()

    asyncio.run(exercise())


def test_unambiguous_claim_conflict_does_not_execute_command() -> None:
    async def exercise():
        generation_id = str(uuid4())
        command = AdminCommand(
            kind="generation_deletion", resource_identity=f"rag:{generation_id}",
            facts_digest="a" * 64, generation_id=generation_id,
        )
        pending = AdminOperation(
            operation_id=uuid4(), kind="generation_deletion", state="pending",
            resource_identity=command.resource_identity,
            progress=AdminProgress(total_items=1), created_at=datetime.now(UTC),
        )
        store = _Store(pending, AdminOperationState.SUCCEEDED, False)
        store.operation = pending.model_copy(update={"state": AdminOperationState.RUNNING})
        calls = 0

        async def handle(_command):
            nonlocal calls
            calls += 1
            return GenerationDeletionResult(deleted_generation_id=generation_id)

        runner = AdminOperationRunner(store=store, clock=_Clock(), handle=handle)
        runner.schedule(pending, command)
        await runner._tasks[pending.operation_id]
        assert calls == 0
        assert store.operation.state is AdminOperationState.RUNNING
        await runner.shutdown()

    asyncio.run(exercise())


def test_old_completion_callback_keeps_replacement_registered_for_shutdown() -> None:
    async def exercise():
        generation_id = str(uuid4())
        command = AdminCommand(
            kind="generation_deletion", resource_identity=f"rag:{generation_id}",
            facts_digest="a" * 64, generation_id=generation_id,
        )
        pending = AdminOperation(
            operation_id=uuid4(), kind="generation_deletion", state="pending",
            resource_identity=command.resource_identity,
            progress=AdminProgress(total_items=1), created_at=datetime.now(UTC),
        )

        class ClaimStore:
            def __init__(self):
                self.attempts = 0

            async def compare_and_swap_state(self, _operation, *, expected):
                self.attempts += 1
                if self.attempts == 1:
                    raise AdminOperationConflictError()
                raise PortUnavailableError("database unavailable")

            async def get(self, _operation_id):
                raise PortUnavailableError("database unavailable")

        store = ClaimStore()
        calls = 0

        async def handle(_command):
            nonlocal calls
            calls += 1

        runner = AdminOperationRunner(store=store, clock=_Clock(), handle=handle)
        runner.schedule(pending, command)
        old_task = runner._tasks[pending.operation_id]
        await asyncio.sleep(0)
        assert old_task.done()
        runner.schedule(pending, command)
        replacement = runner._tasks[pending.operation_id]
        assert replacement is not old_task
        await asyncio.sleep(0)
        assert runner._tasks[pending.operation_id] is replacement
        assert not replacement.done()
        await runner.shutdown()
        assert replacement.done()
        assert replacement.cancelled()
        assert calls == 0

    asyncio.run(exercise())
