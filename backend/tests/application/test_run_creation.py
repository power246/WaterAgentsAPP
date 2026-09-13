import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.creation import (
    CreationCommitUnconfirmedError,
    CreationRolledBackError,
    CreationVerificationUnavailableError,
    IdempotencyKeyConflictError,
    IdempotencyWaitTimeoutError,
    RunCreationCandidate,
)
from app.application.analysis_runs.models import RunStatus
from app.application.analysis_runs.workflow import WorkflowContext
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 13, tzinfo=UTC)


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class RecordingStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[RunCreationCandidate] = []

    async def create_run(self, candidate: RunCreationCandidate):
        self.candidates.append(candidate)
        return await super().create_run(candidate)


class GatedStore(RecordingStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create_run(self, candidate: RunCreationCandidate):
        self.started.set()
        await self.release.wait()
        return await super().create_run(candidate)


class LostCommitStore(RecordingStore):
    def __init__(self, *, verification_gate: asyncio.Event | None = None) -> None:
        super().__init__()
        self.verification_gate = verification_gate
        self.verify_calls = 0

    async def create_run(self, candidate: RunCreationCandidate):
        await super().create_run(candidate)
        raise TimeoutError("commit response was lost")

    async def verify_creation(self, candidate: RunCreationCandidate):
        self.verify_calls += 1
        if self.verification_gate is not None and not self.verification_gate.is_set():
            raise CreationVerificationUnavailableError()
        return await super().verify_creation(candidate)


class RolledBackStore(RecordingStore):
    async def create_run(self, candidate: RunCreationCandidate):
        self.candidates.append(candidate)
        raise TimeoutError("commit outcome was initially unknown")

    async def verify_creation(self, candidate: RunCreationCandidate):
        return None


class TakeoverStore(RecordingStore):
    def __init__(self) -> None:
        super().__init__()
        self.verify_started = asyncio.Event()
        self.verify_release = asyncio.Event()

    async def create_run(self, candidate: RunCreationCandidate):
        self.candidates.append(candidate)
        if len(self.candidates) == 1:
            raise TimeoutError("first transaction outcome was unknown")
        return await InMemoryTranscriptStore.create_run(self, candidate)

    async def verify_creation(self, candidate: RunCreationCandidate):
        self.verify_started.set()
        await self.verify_release.wait()
        return None


def coordinator_for(store, workflow=None, **options) -> RunCoordinator:
    return RunCoordinator(
        FixedClock(),
        transcript_store=store,
        workflow=workflow,
        shutdown_grace_period=0,
        **options,
    )


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


def test_creation_candidate_has_exact_atomic_facts_and_immutable_receipt() -> None:
    async def exercise() -> None:
        store = RecordingStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        raw_body = b'{ "query" : "  lake  ", "time_resolution" : "6h" }'
        key = uuid4()
        try:
            receipt = await coordinator.create_run(raw_body, idempotency_key=key)
            candidate = store.candidates[0]
            assert candidate.request_fingerprint == hashlib.sha256(raw_body).digest()
            assert candidate.query == "lake"
            assert candidate.creation_receipt == receipt.model_dump(mode="json")
            assert set(candidate.creation_receipt) == {
                "run_id", "status", "created_at", "updated_at", "configuration"
            }
            assert [item.transcript_sequence for item in candidate.transcript.items] == [1, 2]
            assert [item.content_type for item in candidate.transcript.items] == [
                "analysis_request", "lifecycle_event"
            ]
            request_body = candidate.transcript.items[0].inline_body
            assert request_body == '{"query":"lake","time_resolution":"6h"}'
            context = await workflow.calls.get()
            assert context.run.run_id == receipt.run_id
            assert context.query == "lake"
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_same_key_concurrent_waiters_share_one_owner_and_one_execution() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        key = uuid4()
        body = b'{"query":"lake"}'
        try:
            first = asyncio.create_task(coordinator.create_run(body, idempotency_key=key))
            await store.started.wait()
            second = asyncio.create_task(coordinator.create_run(body, idempotency_key=key))
            await asyncio.sleep(0)
            assert len(store.candidates) == 0
            assert workflow.calls.empty()
            store.release.set()
            first_receipt, second_receipt = await asyncio.gather(first, second)
            assert first_receipt == second_receipt
            await workflow.calls.get()
            assert len(store.candidates) == 1
            assert workflow.calls.empty()
            assert len(coordinator._runs) == 1
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_in_process_owner_rejects_different_bytes_before_a_second_write() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        key = uuid4()
        first = asyncio.create_task(
            coordinator.create_run(b'{"query":"lake"}', idempotency_key=key)
        )
        try:
            await store.started.wait()
            with pytest.raises(IdempotencyKeyConflictError):
                await coordinator.create_run(b'{ "query":"lake"}', idempotency_key=key)
            store.release.set()
            await first
            await workflow.calls.get()
            assert len(store.candidates) == 1
        finally:
            store.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_lost_commit_response_is_verified_without_rewrite() -> None:
    async def exercise() -> None:
        store = LostCommitStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            receipt = await coordinator.create_run(
                b'{"query":"lake"}', idempotency_key=uuid4()
            )
            context = await workflow.calls.get()
            assert context.run.run_id == receipt.run_id
            assert len(store.candidates) == 1
            assert store.verify_calls == 1
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_unconfirmed_owner_survives_first_response_and_later_converges() -> None:
    async def exercise() -> None:
        verification_gate = asyncio.Event()
        store = LostCommitStore(verification_gate=verification_gate)
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(
            store,
            workflow,
            idempotency_wait_timeout=0.2,
            verification_retry_interval=0.001,
        )
        key = uuid4()
        body = b'{"query":"lake"}'
        try:
            with pytest.raises(CreationCommitUnconfirmedError) as failure:
                await coordinator.create_run(body, idempotency_key=key)
            assert failure.value.run_id == store.candidates[0].snapshot.run_id
            assert workflow.calls.empty()
            assert key in coordinator._creation_owners
            retry = asyncio.create_task(
                coordinator.create_run(body, idempotency_key=key)
            )
            verification_gate.set()
            receipt = await retry
            context = await workflow.calls.get()
            assert context.run.run_id == receipt.run_id == failure.value.run_id
            assert len(store.candidates) == 1
        finally:
            verification_gate.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_confirmed_rollback_never_registers_or_executes() -> None:
    async def exercise() -> None:
        store = RolledBackStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            with pytest.raises(CreationRolledBackError):
                await coordinator.create_run(
                    b'{"query":"lake"}', idempotency_key=uuid4()
                )
            assert coordinator._runs == {}
            assert workflow.calls.empty()
            assert len(store.candidates) == 1
        finally:
            await coordinator.shutdown()

    run(exercise)


def test_waiter_takes_over_only_after_the_first_rollback_is_confirmed() -> None:
    async def exercise() -> None:
        store = TakeoverStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow, idempotency_wait_timeout=0.5)
        key = uuid4()
        body = b'{"query":"lake"}'
        try:
            first = asyncio.create_task(
                coordinator.create_run(body, idempotency_key=key)
            )
            await store.verify_started.wait()
            first_result = (await asyncio.gather(first, return_exceptions=True))[0]
            assert isinstance(first_result, CreationCommitUnconfirmedError)
            waiter = asyncio.create_task(
                coordinator.create_run(body, idempotency_key=key)
            )
            await asyncio.sleep(0)
            assert len(store.candidates) == 1
            store.verify_release.set()
            receipt = await waiter
            context = await workflow.calls.get()
            assert context.run.run_id == receipt.run_id
            assert len(store.candidates) == 2
            assert store.candidates[0].snapshot.run_id != receipt.run_id
        finally:
            store.verify_release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_client_cancellation_does_not_cancel_creation_owner() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        request = asyncio.create_task(
            coordinator.create_run(b'{"query":"lake"}', idempotency_key=uuid4())
        )
        try:
            await store.started.wait()
            request.cancel()
            result = (await asyncio.gather(request, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
            store.release.set()
            context = await workflow.calls.get()
            assert context.run.run_id in coordinator._runs
            assert len(store.candidates) == 1
        finally:
            store.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_owner_and_waiter_timeouts_are_distinct_and_do_not_cancel_owner() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow, idempotency_wait_timeout=0.01)
        key = uuid4()
        body = b'{"query":"lake"}'
        first = asyncio.create_task(coordinator.create_run(body, idempotency_key=key))
        try:
            await store.started.wait()
            first_result = (await asyncio.gather(first, return_exceptions=True))[0]
            assert isinstance(first_result, CreationCommitUnconfirmedError)
            with pytest.raises(IdempotencyWaitTimeoutError):
                await coordinator.create_run(body, idempotency_key=key)
            store.release.set()
            await workflow.calls.get()
            assert len(store.candidates) == 1
        finally:
            store.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_lookup_waits_through_the_post_commit_registration_window() -> None:
    class RegistrationGatedCoordinator(RunCoordinator):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.registration_started = asyncio.Event()
            self.registration_release = asyncio.Event()

        async def _register_committed_creation(self, prepared) -> None:
            self.registration_started.set()
            await self.registration_release.wait()
            await super()._register_committed_creation(prepared)

    async def exercise() -> None:
        store = RecordingStore()
        workflow = HoldingWorkflow()
        coordinator = RegistrationGatedCoordinator(
            FixedClock(), transcript_store=store, workflow=workflow,
            shutdown_grace_period=0,
        )
        creation = asyncio.create_task(coordinator.create_run(b'{"query":"lake"}'))
        try:
            await coordinator.registration_started.wait()
            run_id = store.candidates[0].snapshot.run_id
            lookup = asyncio.create_task(coordinator.get_snapshot(run_id))
            await asyncio.sleep(0)
            assert not lookup.done()
            coordinator.registration_release.set()
            receipt, snapshot = await asyncio.gather(creation, lookup)
            assert snapshot.run_id == receipt.run_id
        finally:
            coordinator.registration_release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_workflow_task_start_failure_keeps_receipt_and_adds_one_failure() -> None:
    async def exercise() -> None:
        store = RecordingStore()
        starts = 0

        def fail_start(coroutine, name):
            nonlocal starts
            del coroutine, name
            starts += 1
            raise RuntimeError("task factory unavailable")

        coordinator = coordinator_for(
            store,
            workflow_task_factory=fail_start,
        )
        key = uuid4()
        body = b'{"query":"lake"}'
        try:
            receipt = await coordinator.create_run(body, idempotency_key=key)
            runtime = coordinator._runs[receipt.run_id]
            assert receipt.status is RunStatus.PENDING
            assert runtime.snapshot.status is RunStatus.FAILED
            assert [event.type.value for event in runtime.events] == [
                "run_created", "run_failed"
            ]
            assert runtime.events[-1].payload.error.code == "workflow_start_failed"
            assert len(runtime.transcript_buffer.items) == 3
            replay = await coordinator.create_run(body, idempotency_key=key)
            assert replay == receipt
            assert starts == 1
        finally:
            await coordinator.shutdown()

    run(exercise)


def test_cancel_wins_if_requested_before_workflow_start_failure_is_recorded() -> None:
    async def exercise() -> None:
        store = RecordingStore()
        coordinator = None

        def cancel_then_fail(coroutine, name):
            del coroutine, name
            runtime = next(iter(coordinator._runs.values()))
            runtime.cancel_requested.set()
            runtime.stop_requested.set()
            raise RuntimeError("task factory unavailable")

        coordinator = coordinator_for(
            store,
            workflow_task_factory=cancel_then_fail,
        )
        try:
            receipt = await coordinator.create_run(b'{"query":"lake"}')
            runtime = coordinator._runs[receipt.run_id]
            assert receipt.status is RunStatus.PENDING
            assert runtime.snapshot.status is RunStatus.CANCELLED
            assert [event.type.value for event in runtime.events] == [
                "run_created", "run_cancelled"
            ]
            assert runtime.last_error is None
        finally:
            await coordinator.shutdown()

    run(exercise)
