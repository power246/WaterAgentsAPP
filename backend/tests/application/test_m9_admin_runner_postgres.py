"""Committed Admin operations recover without request or UI ownership."""

import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from app.domain.settings_admin import AdminCommand
from app.application.settings_admin.admin_operation_runner import AdminOperationRunner
from app.application.settings_admin.errors import AdminOperationConflictError
from app.domain.settings_admin import (
    AdminOperation, AdminOperationState, AdminProgress, GenerationDeletionResult,
)
from app.infrastructure.persistence.admin_operation_repository import (
    PostgresAdminOperationRepository,
)
from app.infrastructure.persistence.database import create_database_engine
from scripts.test_database import temporary_migrated_schema


pytestmark = pytest.mark.skipif(
    not os.environ.get("WATERAGENTS_TEST_DB_NAME"),
    reason="dedicated PostgreSQL test database was not configured",
)


class _Clock:
    def now(self):
        return datetime.now(UTC)


def _entry():
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
    return operation, command


async def _wait_state(store, operation_id, expected):
    for _ in range(200):
        current = await store.get(operation_id)
        if current.state is expected:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


def test_pending_restart_running_reconcile_and_safe_failure() -> None:
    async def exercise(settings):
        engine = create_database_engine(settings)
        store = PostgresAdminOperationRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            pending, command = _entry()
            await store.create(
                pending, idempotency_key=uuid4(),
                request_fingerprint=sha256(b"pending").digest(),
                confirmation_token_digest=sha256(b"pending-token").digest(),
                command=command,
            )
            observed = []

            async def succeed(recovered):
                observed.append(recovered)
                return GenerationDeletionResult(
                    deleted_generation_id=recovered.generation_id,
                )

            restarted = AdminOperationRunner(store=store, clock=_Clock(), handle=succeed)
            await restarted.start()
            completed = await _wait_state(
                store, pending.operation_id, AdminOperationState.SUCCEEDED,
            )
            assert observed == [command]
            assert completed.safe_result.deleted_generation_id == command.generation_id
            await restarted.shutdown()

            interrupted, interrupted_command = _entry()
            await store.create(
                interrupted, idempotency_key=uuid4(),
                request_fingerprint=sha256(b"running").digest(),
                confirmation_token_digest=sha256(b"running-token").digest(),
                command=interrupted_command,
            )
            gate = asyncio.Event()
            entered = asyncio.Event()

            async def block(_):
                entered.set()
                await gate.wait()
                return GenerationDeletionResult(
                    deleted_generation_id=interrupted_command.generation_id,
                )

            owner = AdminOperationRunner(store=store, clock=_Clock(), handle=block)
            owner.schedule(interrupted, interrupted_command)
            await asyncio.wait_for(entered.wait(), 2)
            assert (await store.get(interrupted.operation_id)).state is AdminOperationState.RUNNING
            await owner.shutdown()
            recovery = AdminOperationRunner(store=store, clock=_Clock(), handle=succeed)
            await recovery.start()
            stopped = await store.get(interrupted.operation_id)
            assert stopped.state is AdminOperationState.INTERRUPTED
            assert stopped.safe_error.code == "operation_interrupted"
            with pytest.raises(AdminOperationConflictError):
                await store.compare_and_swap_state(
                    completed, expected=AdminOperationState.SUCCEEDED,
                )

            failed, failed_command = _entry()
            await store.create(
                failed, idempotency_key=uuid4(),
                request_fingerprint=sha256(b"failure").digest(),
                confirmation_token_digest=sha256(b"failure-token").digest(),
                command=failed_command,
            )

            async def fail(_):
                raise RuntimeError("secret provider body must never persist")

            failing = AdminOperationRunner(store=store, clock=_Clock(), handle=fail)
            failing.schedule(failed, failed_command)
            failed_row = await _wait_state(store, failed.operation_id, AdminOperationState.FAILED)
            assert failed_row.safe_error.code == "operation_failed"
            assert "secret provider body" not in failed_row.model_dump_json()
            await failing.shutdown()
            await recovery.shutdown()
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
