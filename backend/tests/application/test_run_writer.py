import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    PublicError,
    RunStatus,
)
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceState,
    PersistenceWriteBatch,
)
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore


class StepClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 9, 13, tzinfo=UTC)

    def now(self) -> datetime:
        value = self._value
        self._value += timedelta(microseconds=1)
        return value


class HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class GatedStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.started: asyncio.Queue[PersistenceWriteBatch] = asyncio.Queue()
        self.release = asyncio.Event()
        self.active = 0
        self.maximum_active = 0
        self.attempts: list[PersistenceWriteBatch] = []

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        self.attempts.append(batch)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.put_nowait(batch)
        try:
            await self.release.wait()
            await super().write_batch(batch)
        finally:
            self.active -= 1


class FailingStore(InMemoryTranscriptStore):
    def __init__(self, failures: int | None) -> None:
        super().__init__()
        self.failures = failures
        self.attempts: list[PersistenceWriteBatch] = []
        self.verify_attempts = 0

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        self.attempts.append(batch)
        if self.failures is None or self.failures > 0:
            if self.failures is not None:
                self.failures -= 1
            raise OSError("database unavailable")
        await super().write_batch(batch)

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        self.verify_attempts += 1
        return await super().verify_batch(batch)


class LostCommitStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.write_attempts = 0
        self.verify_attempts = 0

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        self.write_attempts += 1
        await super().write_batch(batch)
        if self.write_attempts == 1:
            raise TimeoutError("commit response was lost")

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        self.verify_attempts += 1
        return await super().verify_batch(batch)


class PendingSessionLostCommitStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.session_write_attempts = 0
        self.session_verify_attempts = 0
        self.lost_response = False

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        is_session_creation = any(
            mutation.previous_status is None
            for mutation in batch.session_mutations
        )
        await super().write_batch(batch)
        if is_session_creation:
            self.session_write_attempts += 1
            if not self.lost_response:
                self.lost_response = True
                raise OSError("session creation commit response was lost")

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        if any(
            mutation.previous_status is None
            for mutation in batch.session_mutations
        ):
            self.session_verify_attempts += 1
        return await super().verify_batch(batch)


class HangingWriteStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.write_attempts = 0
        self.verify_attempts = 0
        self.write_release = asyncio.Event()
        self.cancelled_write = asyncio.Event()

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        self.write_attempts += 1
        try:
            await self.write_release.wait()
        finally:
            self.cancelled_write.set()
        await super().write_batch(batch)

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        self.verify_attempts += 1
        return await super().verify_batch(batch)


class TerminalFailingStore(InMemoryTranscriptStore):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_attempts = 0

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        if batch.run_mutations and batch.run_mutations[-1].snapshot.status not in {
            RunStatus.PENDING,
            RunStatus.RUNNING,
        }:
            self.terminal_attempts += 1
            raise OSError("terminal write unavailable")
        await super().write_batch(batch)


class PlannedFailureStore(InMemoryTranscriptStore):
    def __init__(self, failed_attempts: set[int]) -> None:
        super().__init__()
        self.failed_attempts = failed_attempts
        self.attempt = 0
        self.attempts: list[PersistenceWriteBatch] = []

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        self.attempt += 1
        self.attempts.append(batch)
        if self.attempt in self.failed_attempts:
            raise OSError("planned persistence failure")
        await super().write_batch(batch)


class RecordingSleep:
    def __init__(self, blocked_delay: float | None = None) -> None:
        self.delays: list[float] = []
        self.blocked_delay = blocked_delay
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if delay == self.blocked_delay:
            self.blocked.set()
            await self.release.wait()
        await asyncio.sleep(0)


def coordinator_for(
    store: InMemoryTranscriptStore,
    workflow: HoldingWorkflow,
    **options,
) -> RunCoordinator:
    return RunCoordinator(
        StepClock(),
        transcript_store=store,
        workflow=workflow,
        shutdown_grace_period=0,
        shutdown_save_grace_period=0,
        persistence_retry_delays=options.pop(
            "persistence_retry_delays",
            (0, 0, 0, 0, 0),
        ),
        **options,
    )


async def wait_until(predicate, timeout: float = 1) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def wait_for_state(
    coordinator: RunCoordinator,
    run_id,
    state: PersistenceState,
) -> None:
    await wait_until(
        lambda: coordinator._transcript_writer.info_locked(
            coordinator._runs[run_id]
        ).state
        is state
    )


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_persistence_attempt_timeout_must_be_finite_and_positive(timeout) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RunCoordinator(
            StepClock(),
            transcript_store=InMemoryTranscriptStore(),
            persistence_attempt_timeout=timeout,
        )


def test_different_runs_write_in_parallel_but_each_run_has_one_writer() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            first = await coordinator.create_run(b'{"query":"first"}')
            second = await coordinator.create_run(b'{"query":"second"}')
            await workflow.calls.get()
            await workflow.calls.get()
            first_batch = await store.started.get()
            second_batch = await store.started.get()
            assert first_batch.run_id != second_batch.run_id
            assert store.maximum_active == 2
            assert coordinator._runs[first.run_id].writer_task is not None
            assert coordinator._runs[second.run_id].writer_task is not None
            store.release.set()
            await wait_for_state(coordinator, first.run_id, PersistenceState.UP_TO_DATE)
            await wait_for_state(coordinator, second.run_id, PersistenceState.UP_TO_DATE)
        finally:
            store.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_same_run_never_overlaps_writes_and_respects_batch_item_target() -> None:
    async def exercise() -> None:
        store = GatedStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_batch_items=2,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await store.started.get()
            for index in range(3):
                identity = await coordinator.reserve_transcript_item(created.run_id)
                await coordinator.finalize_transcript_item(
                    created.run_id,
                    identity.item_id,
                    "analysis_request",
                    1,
                    {"query": f"fact {index}"},
                )
            await asyncio.sleep(0)
            assert len(store.attempts) == 1
            assert store.maximum_active == 1
            store.release.set()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert [len(batch.item_ids) for batch in store.attempts] == [0, 2, 1]
            assert store.maximum_active == 1
        finally:
            store.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_one_run_freezes_failed_batch_before_new_groups_and_never_writes_empty() -> None:
    async def exercise() -> None:
        store = FailingStore(failures=1)
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep(blocked_delay=1)
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await sleeper.blocked.wait()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            assert len(store.attempts) == 1
            frozen = store.attempts[0]
            assert frozen.run_mutations[-1].snapshot.status is RunStatus.RUNNING
            assert not frozen.session_mutations
            sleeper.release.set()
            await wait_until(lambda: len(store.attempts) >= 3)
            assert store.attempts[1] == frozen
            assert any(
                mutation.summary.session_id == session.session_id
                for mutation in store.attempts[2].session_mutations
            )
            await wait_until(lambda: len(store.attempts) >= 4)
            assert store.attempts[3].is_recovery_fact
            assert all(batch.item_ids or batch.mutation_ids for batch in store.attempts)
        finally:
            sleeper.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_higher_sequence_can_persist_while_draft_keeps_state_pending() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            draft = await coordinator.reserve_transcript_item(created.run_id)
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await wait_until(lambda: session.session_id in store._sessions)
            assert max(
                item.transcript_sequence for item in store._items.values()
            ) > draft.transcript_sequence
            info = await coordinator.get_persistence_info(created.run_id)
            assert info.state is PersistenceState.PENDING
            assert info.warning is None
            await coordinator.finalize_transcript_item(
                created.run_id,
                draft.item_id,
                "analysis_request",
                1,
                {"query": "later finalized"},
            )
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_reference_waiting_does_not_block_independent_ready_facts() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            target = await coordinator.reserve_transcript_item(created.run_id)
            dependent = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                dependent.item_id,
                "runtime_fact",
                1,
                {
                    "fact": "late_message_output",
                    "original_item_id": str(target.item_id),
                    "text": "late",
                    "end_reason": "cancelled",
                },
            )
            independent = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                independent.item_id,
                "analysis_request",
                1,
                {"query": "independent"},
            )
            await wait_until(lambda: independent.item_id in store._items)
            assert dependent.item_id not in store._items
            assert (
                await coordinator.get_persistence_info(created.run_id)
            ).state is PersistenceState.PENDING

            await coordinator.finalize_transcript_item(
                created.run_id,
                target.item_id,
                "analysis_request",
                1,
                {"query": "target"},
            )
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert target.item_id in store._items
            assert dependent.item_id in store._items
            assert store._items[target.item_id].transcript_sequence < store._items[
                dependent.item_id
            ].transcript_sequence
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


@pytest.mark.parametrize(
    "terminal",
    ("run_cancel", "run_failure", "session_cancel"),
)
def test_terminal_suppresses_referenced_draft_without_stranding_dependent(
    terminal: str,
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        registry = create_production_registry()
        coordinator = coordinator_for(
            store,
            workflow,
            transcript_registry=registry,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            session = None
            if terminal == "session_cancel":
                session = await coordinator.prepare_agent_session(
                    created.run_id,
                    agent_role=AgentRole.QUERY_PARSER,
                )
                await wait_for_state(
                    coordinator,
                    created.run_id,
                    PersistenceState.UP_TO_DATE,
                )
            target = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id if session is not None else None,
            )
            orphan = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id if session is not None else None,
            )
            dependent = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                dependent.item_id,
                "runtime_fact",
                1,
                {
                    "fact": "late_message_output",
                    "original_item_id": str(target.item_id),
                    "text": "late",
                    "end_reason": "cancelled",
                },
            )
            await asyncio.sleep(0)
            assert dependent.item_id not in store._items

            if terminal == "run_cancel":
                snapshot = await coordinator.cancel_run(created.run_id)
                assert snapshot.status is RunStatus.CANCELLED
                reason = "cancelled"
            elif terminal == "run_failure":
                snapshot = await coordinator.fail_run(
                    created.run_id,
                    PublicError(
                        error_id=UUID(int=1300),
                        code="workflow_failed",
                        message="The workflow failed.",
                        failed_stage="workflow",
                        retryable=False,
                    ),
                )
                assert snapshot.status is RunStatus.FAILED
                reason = "error"
            else:
                assert session is not None
                summary = await coordinator.cancel_agent_session(
                    created.run_id,
                    session.session_id,
                )
                assert summary.status is AgentSessionStatus.CANCELLED
                reason = "cancelled"

            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            info = await coordinator.get_persistence_info(created.run_id)
            runtime = coordinator._runs[created.run_id]
            assert info.warning is None
            assert runtime.writer_task is not None and not runtime.writer_task.done()

            stored = await store.read_run(created.run_id)
            assert stored is not None
            stored_by_id = {item.item_id: item for item in stored.items}
            assert target.item_id in stored_by_id
            assert dependent.item_id in stored_by_id
            assert orphan.item_id not in stored_by_id
            target_row = stored_by_id[target.item_id]
            assert target_row.inline_body is not None
            parsed = registry.parse(target_row, target_row.inline_body)
            assert parsed.parse_status.value == "ok"
            assert parsed.body is not None
            assert parsed.body.model_dump(mode="json") == {
                "fact": "suppressed_transcript_item",
                "reason": reason,
            }
            assert registry.visibility_from_read(parsed).value == "history_only"

            written_ids = [
                item_id
                for batch in store.write_batches
                for item_id in batch.item_ids
            ]
            assert written_ids.index(target.item_id) < written_ids.index(
                dependent.item_id
            )
            original_body = target_row.inline_body
            assert await coordinator.finalize_transcript_item(
                created.run_id,
                target.item_id,
                "analysis_request",
                1,
                {"query": "late finalize must stay suppressed"},
            ) is target
            assert store._items[target.item_id].inline_body == original_body
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_finalize_rejects_unknown_and_cross_run_references_without_drafts() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            first = await coordinator.create_run(b'{"query":"first"}')
            second = await coordinator.create_run(b'{"query":"second"}')
            await workflow.calls.get()
            await workflow.calls.get()
            await wait_for_state(coordinator, first.run_id, PersistenceState.UP_TO_DATE)
            await wait_for_state(coordinator, second.run_id, PersistenceState.UP_TO_DATE)

            foreign_target = await coordinator.reserve_transcript_item(first.run_id)
            rejected_ids = []
            for reference_id, error in (
                (UUID(int=999), "does not exist"),
                (foreign_target.item_id, "same run"),
            ):
                rejected = await coordinator.reserve_transcript_item(second.run_id)
                rejected_ids.append(rejected.item_id)
                with pytest.raises(ValueError, match=error):
                    await coordinator.finalize_transcript_item(
                        second.run_id,
                        rejected.item_id,
                        "runtime_fact",
                        1,
                        {
                            "fact": "late_message_output",
                            "original_item_id": str(reference_id),
                            "text": "late",
                            "end_reason": "cancelled",
                        },
                    )

            identities = await coordinator.list_transcript_items(second.run_id)
            assert not set(rejected_ids).intersection(
                identity.item_id for identity in identities
            )
            await wait_for_state(coordinator, second.run_id, PersistenceState.UP_TO_DATE)
            writer = coordinator._runs[second.run_id].writer_task
            assert writer is not None and not writer.done()

            await coordinator.finalize_transcript_item(
                first.run_id,
                foreign_target.item_id,
                "analysis_request",
                1,
                {"query": "target"},
            )
            await wait_for_state(coordinator, first.run_id, PersistenceState.UP_TO_DATE)
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_unknown_commit_is_verified_without_rewriting_the_frozen_batch() -> None:
    async def exercise() -> None:
        store = LostCommitStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert store.write_attempts == 1
            assert store.verify_attempts == 1
            assert len(store.write_batches) == 1
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_pending_session_creation_unknown_commit_is_verified_in_memory() -> None:
    async def exercise() -> None:
        store = PendingSessionLostCommitStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)

            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)

            assert store.session_write_attempts == 1
            assert store.session_verify_attempts == 1
            assert store._sessions[session.session_id] == session
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_unexpected_writer_failure_is_nonretryable_and_writer_can_recover(
    monkeypatch: pytest.MonkeyPatch,
    application_logs: list[str],
) -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            runtime = coordinator._runs[created.run_id]
            writer = coordinator._transcript_writer
            original = writer._freeze_batch_locked
            failed = False

            def fail_once(candidate):
                nonlocal failed
                if not failed:
                    failed = True
                    raise ValueError("unexpected invalid persistence batch")
                return original(candidate)

            monkeypatch.setattr(
                writer,
                "_freeze_batch_locked",
                fail_once,
            )
            item = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                item.item_id,
                "analysis_request",
                1,
                {"query": "recoverable fact"},
            )
            await wait_for_state(
                coordinator,
                created.run_id,
                PersistenceState.RETRY_EXHAUSTED,
            )

            warning = (await coordinator.get_persistence_info(created.run_id)).warning
            assert warning is not None and warning.retryable is False
            assert runtime.writer_task is not None and not runtime.writer_task.done()
            rows = [
                json.loads(line)
                for line in application_logs
                if json.loads(line)["event_name"] == "persistence_write_failed"
            ]
            assert rows[-1]["error_id"] == str(warning.error_id)
            assert rows[-1]["attempt"] == 1

            retried = await coordinator.retry_persistence(created.run_id)
            assert retried.state is PersistenceState.PENDING
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert runtime.writer_task is not None and not runtime.writer_task.done()
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_persistence_failure_logs_use_warning_error_id_and_actual_attempts(
    application_logs: list[str],
) -> None:
    async def exercise() -> None:
        store = FailingStore(failures=0)
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            store.failures = None
            business_error = PublicError(
                error_id=UUID(int=700),
                code="workflow_failed",
                message="The analysis workflow failed.",
                failed_stage="workflow",
                retryable=False,
            )

            await coordinator.fail_run(created.run_id, business_error)
            await wait_for_state(
                coordinator,
                created.run_id,
                PersistenceState.RETRY_EXHAUSTED,
            )

            runtime = coordinator._runs[created.run_id]
            info = await coordinator.get_persistence_info(created.run_id)
            assert info.warning is not None
            assert runtime.last_error == business_error
            assert info.warning.error_id != business_error.error_id
            rows = [
                json.loads(line)
                for line in application_logs
                if json.loads(line)["event_name"] == "persistence_write_failed"
            ]
            assert [row["attempt"] for row in rows] == [1, 2, 3, 4, 5, 6]
            assert {
                row["error_id"] for row in rows
            } == {str(info.warning.error_id)}
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_attempt_timeout_finishes_old_call_before_backoff_and_shares_budget() -> None:
    async def exercise() -> None:
        store = HangingWriteStore()
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep(blocked_delay=1)
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_attempt_timeout=0.01,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await sleeper.blocked.wait()
            assert store.cancelled_write.is_set()
            assert store.write_attempts == 1
            assert store.verify_attempts == 0

            store.write_release.set()
            sleeper.release.set()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert store.write_attempts == 3
            assert store.verify_attempts == 1
            assert sleeper.delays[:2] == [1, 2]
        finally:
            store.write_release.set()
            sleeper.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_retry_budget_delays_exhaustion_and_manual_recovery_are_stable() -> None:
    async def exercise() -> None:
        store = FailingStore(failures=None)
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep()
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(
                coordinator,
                created.run_id,
                PersistenceState.RETRY_EXHAUSTED,
            )
            assert len(store.attempts) == 6
            assert all(batch == store.attempts[0] for batch in store.attempts)
            assert store.verify_attempts == 6
            assert sleeper.delays == [1, 2, 4, 8, 16]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            attempts = store.verify_attempts
            writes = len(store.attempts)
            runtime = coordinator._runs[created.run_id]
            writer = runtime.writer_task
            await coordinator.get_persistence_info(created.run_id)
            draft = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                draft.item_id,
                "analysis_request",
                1,
                {"query": "new fact"},
            )
            await asyncio.sleep(0)
            assert store.verify_attempts == attempts
            assert len(store.attempts) == writes

            store.failures = 0
            first, second = await asyncio.gather(
                coordinator.retry_persistence(created.run_id),
                coordinator.retry_persistence(created.run_id),
            )
            assert first.state is second.state is PersistenceState.PENDING
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            assert runtime.writer_task is writer
            recovery_facts = [
                item
                for item in runtime.transcript_buffer.items
                if item.content.content_type == "runtime_fact"
                and json.loads(item.content.safe_body)["fact"]
                == "persistence_recovered"
            ]
            assert len(recovery_facts) == 1
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


@pytest.mark.parametrize("recover", [False, True])
def test_completion_uses_third_retry_delivery_window(recover: bool) -> None:
    async def exercise() -> None:
        store = FailingStore(failures=None)
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep(blocked_delay=8)
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
            completion_delivery_grace=0.05,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await sleeper.blocked.wait()
            if recover:
                store.failures = 0
                sleeper.release.set()
            result = await coordinator.complete_run(created.run_id)
            assert result.status is (
                RunStatus.COMPLETED
                if recover
                else RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR
            )
        finally:
            sleeper.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_completion_does_not_restart_an_expired_delivery_window() -> None:
    async def exercise() -> None:
        store = FailingStore(failures=None)
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep(blocked_delay=8)
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
            completion_delivery_grace=0.01,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await sleeper.blocked.wait()
            await asyncio.sleep(0.02)
            async with asyncio.timeout(0.2):
                result = await coordinator.complete_run(created.run_id)
            assert result.status is RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR
        finally:
            sleeper.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_cancel_wins_during_completion_delivery_window() -> None:
    async def exercise() -> None:
        store = FailingStore(failures=None)
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep(blocked_delay=8)
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_retry_delays=(1, 2, 4, 8, 16),
            persistence_sleep=sleeper,
            completion_delivery_grace=1,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await sleeper.blocked.wait()
            completion = asyncio.create_task(coordinator.complete_run(created.run_id))
            await asyncio.sleep(0)
            cancelled = await coordinator.cancel_run(created.run_id)
            assert await completion is cancelled
            assert cancelled.status is RunStatus.CANCELLED
        finally:
            sleeper.release.set()
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_terminal_write_failure_never_reselects_business_terminal() -> None:
    async def exercise() -> None:
        store = TerminalFailingStore()
        workflow = HoldingWorkflow()
        sleeper = RecordingSleep()
        coordinator = coordinator_for(
            store,
            workflow,
            persistence_sleep=sleeper,
        )
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            completed = await coordinator.complete_run(created.run_id)
            await wait_for_state(
                coordinator,
                created.run_id,
                PersistenceState.RETRY_EXHAUSTED,
            )
            assert completed.status is RunStatus.COMPLETED
            assert (await coordinator.get_snapshot(created.run_id)) is completed
            assert len(coordinator._runs[created.run_id].events) == 2
            assert store.terminal_attempts == 6
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)


def test_recovery_fact_failure_does_not_recursively_add_recovery_facts() -> None:
    async def exercise() -> None:
        store = PlannedFailureStore(failed_attempts={1, 3})
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            created = await coordinator.create_run(b'{"query":"lake"}')
            await workflow.calls.get()
            await wait_for_state(coordinator, created.run_id, PersistenceState.UP_TO_DATE)
            facts = [
                item
                for item in coordinator._runs[created.run_id].transcript_buffer.items
                if item.content.content_type == "runtime_fact"
            ]
            assert len(facts) == 1
            assert len(
                [batch for batch in store.attempts if batch.is_recovery_fact]
            ) == 2
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    run(exercise)
