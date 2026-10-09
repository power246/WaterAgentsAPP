"""Client cancellation cannot strand an already committed Admin acceptance."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.settings_admin.admin_service import AdminService
from app.application.settings_admin.admin_operation_runner import AdminOperationRunner
from app.application.settings_admin.errors import (
    AdminOperationConflictError, HistoryCleanupSelectionTooLargeError,
)
from app.domain.settings_admin import (
    AdminCommand, AdminOperationState, GenerationDeletionResult,
)
from app.ports.errors import PortUnavailableError


class _Clock:
    def now(self):
        return datetime.now(UTC)


class _CommitGate:
    def __init__(self):
        self.committed = asyncio.Event()
        self.release = asyncio.Event()
        self.saved = None

    async def read_receipt(self, _key):
        return None

    async def create(self, operation, **_kwargs):
        self.saved = operation
        self.committed.set()
        await self.release.wait()
        return operation


def test_disconnect_after_commit_still_schedules_the_durable_operation() -> None:
    async def exercise():
        store = _CommitGate()
        service = AdminService(
            settings=object(), maintenance=object(), store=store, clock=_Clock(),
            profile_for_id=lambda _id: None,
            apply_profile=lambda _profile: None,
            notify_maintenance=lambda: None,
        )
        command = AdminCommand(
            kind="generation_deletion", resource_identity=f"rag:{uuid4()}",
            facts_digest="a" * 64, generation_id=str(uuid4()),
        )

        async def same_facts(_):
            return command.facts_digest

        service._current_digest = same_facts
        scheduled = []
        service.bind_runner(lambda operation, recovered: scheduled.append(
            (operation, recovered)
        ))
        confirmation = await service._confirmations.issue(
            action=command.kind.value, phrase="DELETE RAG GENERATION",
            payload=command, now=_Clock().now(),
        )
        request = asyncio.create_task(service.execute(
            kind=command.kind, token=confirmation.token,
            phrase=confirmation.required_phrase, idempotency_key=uuid4(),
            raw_body=b"{}", generation_id=command.generation_id,
        ))
        await asyncio.wait_for(store.committed.wait(), 2)
        request.cancel()
        assert (await asyncio.gather(request, return_exceptions=True))[0].__class__ is asyncio.CancelledError
        store.release.set()
        await service.drain_accepts()
        assert store.saved.state is AdminOperationState.PENDING
        assert scheduled == [(store.saved, command)]

    asyncio.run(exercise())


def test_same_key_retry_schedules_a_committed_admin_command_without_restart() -> None:
    class LostAcceptanceStore:
        def __init__(self):
            self.saved = None
            self.command = None
            self.fingerprint = None
            self.read_failed = False

        async def read_receipt(self, _key):
            if self.saved is None:
                return None
            if not self.read_failed:
                self.read_failed = True
                raise PortUnavailableError("receipt temporarily unavailable")
            return self.saved, self.fingerprint, self.command

        async def create(self, operation, *, request_fingerprint, command, **_kwargs):
            self.saved = operation
            self.fingerprint = request_fingerprint
            self.command = command
            raise PortUnavailableError("commit acknowledgement lost")

        async def get(self, _operation_id):
            return self.saved

        async def compare_and_swap_state(self, operation, *, expected):
            if self.saved.state is not expected:
                raise AdminOperationConflictError()
            self.saved = operation
            return operation

    async def exercise():
        store = LostAcceptanceStore()
        generation_id = str(uuid4())
        command = AdminCommand(
            kind="generation_deletion", resource_identity=f"rag:{generation_id}",
            facts_digest="a" * 64, generation_id=generation_id,
        )
        service = AdminService(
            settings=object(), maintenance=object(), store=store, clock=_Clock(),
            profile_for_id=lambda _id: None,
            apply_profile=lambda _profile: None,
            notify_maintenance=lambda: None,
        )

        async def same_facts(_):
            return command.facts_digest

        service._current_digest = same_facts
        handled = []

        async def handle(recovered):
            handled.append(recovered)
            return GenerationDeletionResult(deleted_generation_id=generation_id)

        runner = AdminOperationRunner(store=store, clock=_Clock(), handle=handle)
        service.bind_runner(runner.schedule)
        confirmation = await service._confirmations.issue(
            action=command.kind.value, phrase="DELETE RAG GENERATION",
            payload=command, now=_Clock().now(),
        )
        key = uuid4()
        request = dict(
            kind=command.kind, token=confirmation.token,
            phrase=confirmation.required_phrase,
            idempotency_key=key, raw_body=b"same request",
            generation_id=generation_id,
        )
        with pytest.raises(PortUnavailableError):
            await service.execute(**request)
        assert store.saved.state is AdminOperationState.PENDING
        retried = await service.execute(**request)
        assert retried.operation_id == store.saved.operation_id
        for _ in range(100):
            if store.saved.state is AdminOperationState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert store.saved.state is AdminOperationState.SUCCEEDED
        assert handled == [command]
        await runner.shutdown()

    asyncio.run(exercise())


def test_all_history_selection_rejects_over_limit_before_confirmation() -> None:
    class _TooMany:
        async def list_history_run_ids(self, *, limit):
            assert limit == 501
            return tuple(str(uuid4()) for _ in range(501))

    async def exercise():
        service = AdminService(
            settings=object(), maintenance=_TooMany(), store=object(),
            clock=_Clock(), profile_for_id=lambda _id: None,
            apply_profile=lambda _profile: None,
            notify_maintenance=lambda: None,
        )
        with pytest.raises(HistoryCleanupSelectionTooLargeError):
            await service.preview_history(run_ids=None, cascade_consumers=True)
        assert not service._confirmations._claims

    asyncio.run(exercise())
