import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.creation import (
    CreationCommitUnconfirmedError,
    IdempotencyKeyConflictError,
    RunCreationCandidate,
)
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionStateMutation,
    SessionSummary,
    TranscriptItemIdentity,
)
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceGroup,
    PersistenceState,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
)
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.transcripts.registry import create_production_registry
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import (
    TranscriptContentUnreadableError,
    TranscriptReader,
)
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.persistence.database import (
    check_database_schema,
    create_database_engine,
    upgrade_database,
)
from app.infrastructure.persistence.records import (
    PersistenceBatch,
    PersistedRunRecord,
    PersistedSessionRecord,
    PersistedTranscriptContentRecord,
    PersistedTranscriptItemRecord,
)
from app.infrastructure.persistence.schema import metadata, transcript_items
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_contents,
)
from app.infrastructure.persistence.store import PostgresTranscriptStore
from app.infrastructure.runtime.system_clock import SystemClock
from app.main import create_app
from scripts.test_database import temporary_migrated_schema


@pytest.fixture(scope="module")
def postgres_settings() -> Iterator[AppSettings]:
    with temporary_migrated_schema() as settings:
        yield settings


class RollbackOncePostgresStore(PostgresTranscriptStore):
    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        super().__init__(
            engine,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.fail_item_id: UUID | None = None
        self.rollback_observed = False
        self.failed_attempts = 0
        self.committed_batches: list[PersistenceWriteBatch] = []

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        if (
            self.fail_item_id is not None
            and self.fail_item_id in batch.item_ids
            and self.failed_attempts == 0
        ):
            self.failed_attempts += 1
            try:
                async with self._run_transaction(batch.run_id) as connection:
                    await self._apply_state_mutations(connection, batch)
                    await self._execute_transcript_batch(connection, batch)
                    raise OSError("injected rollback after batch writes")
            except OSError:
                async with self._engine.connect() as connection:
                    stored_item = (
                        await connection.execute(
                            select(transcript_items.c.item_id).where(
                                transcript_items.c.item_id == self.fail_item_id
                            )
                        )
                    ).scalar_one_or_none()
                    stored_status = await connection.scalar(
                        select(analysis_runs.c.status).where(
                            analysis_runs.c.run_id == batch.run_id
                        )
                    )
                self.rollback_observed = (
                    stored_item is None and stored_status == "running"
                )
                raise
        await super().write_batch(batch)
        self.committed_batches.append(batch)


class NoLiveTranscript:
    async def get_live_read(self, run_id: UUID | str):
        del run_id
        return None


def _item(
    *,
    run_id: UUID,
    item_id: UUID,
    sequence: int,
    session_id: UUID | None = None,
    body: str = "{}",
    content_type: str = "fixture_message",
) -> PersistedTranscriptItemRecord:
    return PersistedTranscriptItemRecord(
        item_id=item_id,
        run_id=run_id,
        session_id=session_id,
        transcript_sequence=sequence,
        created_at=datetime.now(UTC),
        content_type=content_type,
        content_schema_version=1,
        inline_body=body,
        content_bytes=len(body.encode("utf-8")),
    )


def _run_batch(
    *,
    idempotency_key: UUID | None = None,
) -> tuple[UUID, PersistenceBatch]:
    run_id = uuid4()
    request_item_id = uuid4()
    created_item_id = uuid4()
    now = datetime.now(UTC)
    run = PersistedRunRecord(
        run_id=run_id,
        status="pending",
        created_at=now,
        updated_at=now,
        query="integration query",
        idempotency_key=idempotency_key,
        request_fingerprint=b"x" * 32 if idempotency_key else None,
        creation_receipt={"run_id": str(run_id)},
        request_item_id=request_item_id,
        created_item_id=created_item_id,
    )
    return run_id, PersistenceBatch(
        runs=(run,),
        items=(
            _item(
                run_id=run_id,
                item_id=request_item_id,
                sequence=1,
                content_type="analysis_request",
            ),
            _item(
                run_id=run_id,
                item_id=created_item_id,
                sequence=2,
                content_type="lifecycle_event",
            ),
        ),
    )


async def _commit(settings: AppSettings, batch: PersistenceBatch) -> None:
    engine = create_database_engine(settings)
    try:
        await PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        ).commit_batch(batch)
    finally:
        await engine.dispose()


async def _wait_persistence(
    coordinator: RunCoordinator,
    run_id: UUID,
) -> None:
    runtime = coordinator._runs[run_id]
    async with asyncio.timeout(10):
        async with runtime.condition:
            await runtime.condition.wait_for(
                lambda: coordinator._persistence_info_locked(runtime).state
                is not PersistenceState.PENDING
            )
            info = coordinator._persistence_info_locked(runtime)
    if info.state is not PersistenceState.UP_TO_DATE:
        raise AssertionError(info)


def test_upgrade_check_and_repeated_upgrade_on_real_postgres(
    postgres_settings: AppSettings,
) -> None:
    assert asyncio.run(check_database_schema(postgres_settings)) == "20260913_01"
    assert upgrade_database(postgres_settings) == "20260913_01"

    async def inspect_schema() -> tuple[set[str], set[str], list[object]]:
        engine = create_database_engine(postgres_settings)
        try:
            async with engine.connect() as connection:
                tables, constraints, schema_diff = await connection.run_sync(
                    lambda sync_connection: (
                        set(inspect(sync_connection).get_table_names()),
                        {
                            constraint["name"]
                            for table in (
                                "analysis_runs",
                                "analysis_sessions",
                                "transcript_items",
                                "transcript_contents",
                            )
                            for constraint in inspect(
                                sync_connection
                            ).get_check_constraints(table)
                        },
                        compare_metadata(
                            MigrationContext.configure(sync_connection),
                            metadata,
                        ),
                    )
                )
                return tables, constraints, schema_diff
        finally:
            await engine.dispose()

    tables, constraints, schema_diff = asyncio.run(inspect_schema())
    assert tables == {
        "alembic_version",
        "analysis_runs",
        "analysis_sessions",
        "transcript_items",
        "transcript_contents",
    }
    assert "ck_analysis_runs_terminal_item_matches_status" in constraints
    assert "ck_analysis_sessions_role_comid" in constraints
    assert "ck_transcript_items_inline_or_preview" in constraints
    assert schema_diff == []


def test_database_rejects_duplicate_key_sequence_and_agent_identity(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        key = uuid4()
        run_id, first = _run_batch(idempotency_key=key)
        await _commit(postgres_settings, first)

        _, duplicate_key = _run_batch(idempotency_key=key)
        with pytest.raises(IntegrityError):
            await _commit(postgres_settings, duplicate_key)

        duplicate_sequence = _item(
            run_id=run_id,
            item_id=uuid4(),
            sequence=1,
        )
        would_be_partial = _item(
            run_id=run_id,
            item_id=uuid4(),
            sequence=3,
        )
        with pytest.raises(IntegrityError):
            await _commit(
                postgres_settings,
                PersistenceBatch(items=(would_be_partial, duplicate_sequence)),
            )
        engine = create_database_engine(postgres_settings)
        try:
            assert await _count_items(engine, (would_be_partial.item_id,)) == 0
        finally:
            await engine.dispose()

        first_session_id = uuid4()
        first_created_id = uuid4()
        now = datetime.now(UTC)
        first_session = PersistedSessionRecord(
            session_id=first_session_id,
            run_id=run_id,
            agent_role="query_parser",
            status="pending",
            created_at=now,
            updated_at=now,
            created_item_id=first_created_id,
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(
                sessions=(first_session,),
                items=(
                    _item(
                        run_id=run_id,
                        session_id=first_session_id,
                        item_id=first_created_id,
                        sequence=3,
                        content_type="lifecycle_event",
                    ),
                ),
            ),
        )

        second_session_id = uuid4()
        second_created_id = uuid4()
        second_session = first_session.model_copy(
            update={
                "session_id": second_session_id,
                "created_item_id": second_created_id,
            }
        )
        with pytest.raises(IntegrityError):
            await _commit(
                postgres_settings,
                PersistenceBatch(
                    sessions=(second_session,),
                    items=(
                        _item(
                            run_id=run_id,
                            session_id=second_session_id,
                            item_id=second_created_id,
                            sequence=4,
                            content_type="lifecycle_event",
                        ),
                    ),
                ),
            )

        waterbody_session_id = uuid4()
        waterbody_created_id = uuid4()
        waterbody = PersistedSessionRecord(
            session_id=waterbody_session_id,
            run_id=run_id,
            agent_role="waterbody",
            target_comid="12345",
            status="pending",
            created_at=now,
            updated_at=now,
            created_item_id=waterbody_created_id,
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(
                sessions=(waterbody,),
                items=(
                    _item(
                        run_id=run_id,
                        session_id=waterbody_session_id,
                        item_id=waterbody_created_id,
                        sequence=5,
                        content_type="lifecycle_event",
                    ),
                ),
            ),
        )
        duplicate_waterbody_id = uuid4()
        duplicate_waterbody_item_id = uuid4()
        duplicate_waterbody = waterbody.model_copy(
            update={
                "session_id": duplicate_waterbody_id,
                "created_item_id": duplicate_waterbody_item_id,
            }
        )
        with pytest.raises(IntegrityError):
            await _commit(
                postgres_settings,
                PersistenceBatch(
                    sessions=(duplicate_waterbody,),
                    items=(
                        _item(
                            run_id=run_id,
                            session_id=duplicate_waterbody_id,
                            item_id=duplicate_waterbody_item_id,
                            sequence=6,
                            content_type="lifecycle_event",
                        ),
                    ),
                ),
            )

    asyncio.run(exercise())


def test_concurrent_coordinators_use_database_key_authority_and_execute_once(
    postgres_settings: AppSettings,
) -> None:
    class Clock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    class Workflow:
        def __init__(self) -> None:
            self.calls: list[WorkflowContext] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.append(context)
            self.started.set()
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise() -> None:
        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        workflow = Workflow()
        coordinators = [
            RunCoordinator(
                Clock(),
                transcript_store=store,
                workflow=workflow,
                shutdown_grace_period=0,
            )
            for _ in range(2)
        ]
        key = uuid4()
        body = b'{"query":"database race"}'
        try:
            receipts = await asyncio.gather(
                *(
                    coordinator.create_run(body, idempotency_key=key)
                    for coordinator in coordinators
                )
            )
            assert receipts[0] == receipts[1]
            await workflow.started.wait()
            assert len(workflow.calls) == 1
            assert sum(len(coordinator._runs) for coordinator in coordinators) == 1
            stored = await store.get_creation(key)
            assert stored is not None
            assert stored.creation.receipt_snapshot() == receipts[0]
            with pytest.raises(IdempotencyKeyConflictError):
                await coordinators[0].create_run(
                    b'{ "query":"database race"}',
                    idempotency_key=key,
                )
        finally:
            workflow.release.set()
            await asyncio.gather(
                *(coordinator.shutdown() for coordinator in coordinators)
            )
            await engine.dispose()

    asyncio.run(exercise())


def test_creation_verification_maps_external_content_by_expected_item_id(
    postgres_settings: AppSettings,
) -> None:
    class LostCreateResponseStore(PostgresTranscriptStore):
        async def create_run(self, candidate: RunCreationCandidate):
            await super().create_run(candidate)
            raise OSError("connection closed after commit")

    class Clock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise() -> None:
        engine = create_database_engine(postgres_settings)
        store = LostCreateResponseStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        workflow = Workflow()
        record_ids = iter(UUID(int=value) for value in (2, 1, 3, 4, 5, 6, 7, 8))
        coordinator = RunCoordinator(
            Clock(),
            transcript_store=store,
            workflow=workflow,
            record_id_factory=record_ids.__next__,
            inline_content_bytes=1,
            preview_bytes=1,
            shutdown_grace_period=0,
        )
        key = uuid4()
        body = b'{"query":"external creation content"}'
        try:
            try:
                receipt = await coordinator.create_run(body, idempotency_key=key)
            except CreationCommitUnconfirmedError:
                receipt = await coordinator.create_run(body, idempotency_key=key)
            context = await workflow.calls.get()
            assert context.run.run_id == receipt.run_id
            replay = await coordinator.create_run(body, idempotency_key=key)
            assert replay == receipt
            assert workflow.calls.empty()

            stored = await store.read_run(receipt.run_id)
            assert stored is not None
            assert [item.item_id for item in stored.items] == [UUID(int=2), UUID(int=1)]
            assert all(item.inline_body is None for item in stored.items)
            assert len(stored.contents) == 2
        finally:
            workflow.release.set()
            await coordinator.shutdown()
            await engine.dispose()

    asyncio.run(exercise())


def test_run_writer_persists_incremental_state_events_and_large_content(
    postgres_settings: AppSettings,
) -> None:
    class Clock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise() -> None:
        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        workflow = Workflow()
        coordinator = RunCoordinator(
            Clock(),
            transcript_store=store,
            workflow=workflow,
            persistence_retry_delays=(0, 0, 0, 0, 0),
            shutdown_grace_period=0,
            shutdown_save_grace_period=1,
        )
        try:
            created = await coordinator.create_run(b'{"query":"writer integration"}')
            await workflow.calls.get()
            await _wait_persistence(coordinator, created.run_id)
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            await coordinator.complete_agent_session(
                created.run_id,
                session.session_id,
            )
            large = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                large.item_id,
                "analysis_request",
                1,
                {"query": "水" * 30000},
            )
            await _wait_persistence(coordinator, created.run_id)
            completed = await coordinator.complete_run(created.run_id)
            await _wait_persistence(coordinator, created.run_id)

            async with engine.connect() as connection:
                run_row = (
                    await connection.execute(
                        select(analysis_runs).where(
                            analysis_runs.c.run_id == created.run_id
                        )
                    )
                ).mappings().one()
                session_row = (
                    await connection.execute(
                        select(analysis_sessions).where(
                            analysis_sessions.c.session_id == session.session_id
                        )
                    )
                ).mappings().one()
                item_rows = tuple(
                    (
                        await connection.execute(
                            select(transcript_items)
                            .where(transcript_items.c.run_id == created.run_id)
                            .order_by(transcript_items.c.transcript_sequence)
                        )
                    ).mappings()
                )
                content_row = (
                    await connection.execute(
                        select(transcript_contents).where(
                            transcript_contents.c.item_id == large.item_id
                        )
                    )
                ).mappings().one()
            assert run_row["status"] == completed.status.value == "completed"
            assert run_row["terminal_item_id"] == item_rows[-1]["item_id"]
            assert session_row["status"] == "completed"
            assert session_row["terminal_item_id"] is not None
            assert [row["transcript_sequence"] for row in item_rows] == list(
                range(1, 7)
            )
            assert content_row["body"].count("水") == 30000
        finally:
            workflow.release.set()
            await coordinator.shutdown()
            await engine.dispose()

    asyncio.run(exercise())


def test_pending_session_creation_unknown_commit_matches_memory_contract(
    postgres_settings: AppSettings,
) -> None:
    class LostPendingSessionCommitStore(PostgresTranscriptStore):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.session_write_attempts = 0
            self.session_verify_attempts = 0
            self.lost_response = False
            self.session_batch: PersistenceWriteBatch | None = None

        async def write_batch(self, batch: PersistenceWriteBatch) -> None:
            is_session_creation = any(
                mutation.previous_status is None
                for mutation in batch.session_mutations
            )
            await super().write_batch(batch)
            if is_session_creation:
                self.session_batch = batch
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

    class Clock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise() -> None:
        engine = create_database_engine(postgres_settings)
        store = LostPendingSessionCommitStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        workflow = Workflow()
        coordinator = RunCoordinator(
            Clock(),
            transcript_store=store,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        try:
            created = await coordinator.create_run(b'{"query":"session verify"}')
            await workflow.calls.get()
            await _wait_persistence(coordinator, created.run_id)

            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await _wait_persistence(coordinator, created.run_id)

            assert store.session_write_attempts == 1
            assert store.session_verify_attempts == 1
            assert store.session_batch is not None
            assert len(store.session_batch.item_ids) == 1
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            analysis_sessions.c.status,
                            analysis_sessions.c.created_item_id,
                            analysis_sessions.c.terminal_item_id,
                        ).where(
                            analysis_sessions.c.session_id == session.session_id
                        )
                    )
                ).mappings().one()
            assert row["status"] == "pending"
            assert row["created_item_id"] == store.session_batch.item_ids[0]
            assert row["terminal_item_id"] is None
        finally:
            workflow.release.set()
            await coordinator.shutdown()
            await engine.dispose()

    asyncio.run(exercise())


def test_incremental_writer_reuses_one_engine_across_batches(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        try:
            await store.commit_batch(creation)
            for sequence in (3, 4):
                item = _item(
                    run_id=run_id,
                    item_id=uuid4(),
                    sequence=sequence,
                )
                group = PersistenceGroup(item_ids=(item.item_id,))
                await store.write_batch(PersistenceWriteBatch(
                    run_id=run_id,
                    groups=(group,),
                    transcript={"items": (item,), "contents": ()},
                ))
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_referenced_cancelled_draft_rolls_back_retries_and_survives_restart(
    postgres_settings: AppSettings,
) -> None:
    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise() -> None:
        engine = create_database_engine(postgres_settings)
        store = RollbackOncePostgresStore(
            engine,
            operation_timeout_seconds=(
                postgres_settings.db_operation_timeout_seconds
            ),
        )
        workflow = Workflow()
        registry = create_production_registry()
        coordinator = RunCoordinator(
            SystemClock(),
            transcript_store=store,
            workflow=workflow,
            shutdown_grace_period=0,
            shutdown_save_grace_period=0,
            persistence_retry_delays=(0, 0, 0, 0, 0),
            transcript_registry=registry,
        )
        try:
            created = await coordinator.create_run(
                b'{"query":"referenced cancellation"}'
            )
            await workflow.calls.get()
            await _wait_persistence(coordinator, created.run_id)
            target = await coordinator.reserve_transcript_item(created.run_id)
            orphan = await coordinator.reserve_transcript_item(created.run_id)
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
            store.fail_item_id = target.item_id
            cancelled = await coordinator.cancel_run(created.run_id)
            assert cancelled.status is RunStatus.CANCELLED
            await _wait_persistence(coordinator, created.run_id)

            assert store.failed_attempts == 1
            assert store.rollback_observed
            committed_ids = [
                item_id
                for batch in store.committed_batches
                for item_id in batch.item_ids
            ]
            assert committed_ids.index(target.item_id) < committed_ids.index(
                dependent.item_id
            )
            info = await coordinator.get_persistence_info(created.run_id)
            assert info.state is PersistenceState.UP_TO_DATE
            assert info.warning is None
            assert await coordinator.finalize_transcript_item(
                created.run_id,
                target.item_id,
                "analysis_request",
                1,
                {"query": "late finalize must stay suppressed"},
            ) is target
        finally:
            workflow.release.set()
            await coordinator.shutdown()
            await engine.dispose()

        restarted_engine = create_database_engine(postgres_settings)
        restarted_store = PostgresTranscriptStore(
            restarted_engine,
            operation_timeout_seconds=(
                postgres_settings.db_operation_timeout_seconds
            ),
        )
        try:
            stored = await restarted_store.read_run(created.run_id)
            assert stored is not None
            assert stored.run.status is RunStatus.CANCELLED
            stored_by_id = {item.item_id: item for item in stored.items}
            assert target.item_id in stored_by_id
            assert dependent.item_id in stored_by_id
            assert orphan.item_id not in stored_by_id
            target_row = stored_by_id[target.item_id]
            assert target_row.inline_body is not None
            assert json.loads(target_row.inline_body) == {
                "fact": "suppressed_transcript_item",
                "reason": "cancelled",
            }
            parsed = registry.parse(target_row, target_row.inline_body)
            assert parsed.parse_status.value == "ok"
            assert registry.visibility_from_read(parsed).value == "history_only"
        finally:
            await restarted_engine.dispose()

    asyncio.run(exercise())


def test_incremental_batch_rolls_back_session_event_and_full_content_together(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        await _commit(postgres_settings, creation)
        session_id = uuid4()
        item_id = uuid4()
        timestamp = datetime.now(UTC)
        identity = TranscriptItemIdentity(
            item_id=item_id,
            run_id=run_id,
            session_id=session_id,
            transcript_sequence=1,
            created_at=timestamp,
        )
        summary = SessionSummary(
            session_id=session_id,
            agent_role=AgentRole.QUERY_PARSER,
            status=AgentSessionStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
        )
        mutation = SessionStateMutation(
            mutation_id=uuid4(),
            run_id=run_id,
            previous_status=None,
            summary=summary,
            transcript_item=identity,
        )
        body = '{"payload":"' + "x" * 70000 + '"}'
        item = PersistedTranscriptItemRecord(
            **identity.model_dump(mode="python"),
            content_type="lifecycle_event",
            content_schema_version=2,
            preview_head=body[:32],
            preview_tail=body[-32:],
            content_bytes=len(body.encode("utf-8")),
        )
        content = PersistedTranscriptContentRecord(
            item_id=item_id,
            body=body,
            created_at=timestamp,
        )
        group = PersistenceGroup(
            item_ids=(item_id,),
            mutation_ids=(mutation.mutation_id,),
        )
        batch = PersistenceWriteBatch(
            run_id=run_id,
            groups=(group,),
            transcript={"items": (item,), "contents": (content,)},
            session_mutations=(mutation,),
        )
        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        try:
            with pytest.raises(PersistenceWriteConflictError):
                await store.write_batch(batch)
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(analysis_sessions.c.session_id).where(
                            analysis_sessions.c.session_id == session_id
                        )
                    )
                ) is None
                assert (
                    await connection.scalar(
                        select(transcript_items.c.item_id).where(
                            transcript_items.c.item_id == item_id
                        )
                    )
                ) is None
                assert (
                    await connection.scalar(
                        select(transcript_contents.c.item_id).where(
                            transcript_contents.c.item_id == item_id
                        )
                    )
                ) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_database_rejects_cross_run_session_ownership_and_invalid_status(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        first_run_id, first_batch = _run_batch()
        second_run_id, second_batch = _run_batch()
        await _commit(postgres_settings, first_batch)
        await _commit(postgres_settings, second_batch)

        session_id = uuid4()
        created_item_id = uuid4()
        now = datetime.now(UTC)
        session = PersistedSessionRecord(
            session_id=session_id,
            run_id=first_run_id,
            agent_role="waterbody",
            target_comid="123",
            status="pending",
            created_at=now,
            updated_at=now,
            created_item_id=created_item_id,
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(
                sessions=(session,),
                items=(
                    _item(
                        run_id=first_run_id,
                        session_id=session_id,
                        item_id=created_item_id,
                        sequence=3,
                        content_type="lifecycle_event",
                    ),
                ),
            ),
        )

        cross_run_item = _item(
            run_id=second_run_id,
            session_id=session_id,
            item_id=uuid4(),
            sequence=3,
        )
        with pytest.raises(IntegrityError):
            await _commit(
                postgres_settings,
                PersistenceBatch(items=(cross_run_item,)),
            )

        engine = create_database_engine(postgres_settings)
        try:
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE analysis_runs SET status = 'invalid' "
                            "WHERE run_id = :run_id"
                        ),
                        {"run_id": first_run_id},
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_adapter_rejects_invalid_external_batch_without_partial_insert(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, batch = _run_batch()
        await _commit(postgres_settings, batch)
        valid_item = _item(run_id=run_id, item_id=uuid4(), sequence=3)
        external_item = PersistedTranscriptItemRecord(
            item_id=uuid4(),
            run_id=run_id,
            transcript_sequence=4,
            created_at=datetime.now(UTC),
            content_type="fixture_message",
            content_schema_version=1,
            preview_head="head",
            preview_tail="tail",
            content_bytes=4,
        )
        bypassed = PersistenceBatch.model_construct(
            runs=(),
            sessions=(),
            items=(valid_item, external_item),
            contents=(),
        )
        engine = create_database_engine(postgres_settings)
        try:
            store = PostgresTranscriptStore(engine, operation_timeout_seconds=5)
            with pytest.raises(ValidationError, match="requires transcript_contents"):
                await store.commit_batch(bypassed)
            count = await _count_items(
                engine,
                (valid_item.item_id, external_item.item_id),
            )
            assert count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_external_content_is_committed_atomically(postgres_settings: AppSettings) -> None:
    async def exercise() -> None:
        run_id, batch = _run_batch()
        await _commit(postgres_settings, batch)
        item_id = uuid4()
        now = datetime.now(UTC)
        body = '{"message":"湖"}'
        item = PersistedTranscriptItemRecord(
            item_id=item_id,
            run_id=run_id,
            transcript_sequence=3,
            created_at=now,
            content_type="fixture_message",
            content_schema_version=1,
            preview_head="head",
            preview_tail="tail",
            content_bytes=len(body.encode("utf-8")),
        )
        content = PersistedTranscriptContentRecord(
            item_id=item_id,
            body=body,
            created_at=now,
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(items=(item,), contents=(content,)),
        )

        engine = create_database_engine(postgres_settings)
        try:
            async with engine.connect() as connection:
                stored = await connection.scalar(
                    text("SELECT body FROM transcript_contents WHERE item_id = :item_id"),
                    {"item_id": item_id},
                )
            assert stored == body
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("external", [False, True], ids=["inline", "external"])
def test_real_postgres_corrupt_json_has_safe_fulltext_when_readable(
    postgres_settings: AppSettings,
    external: bool,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        unsafe_values = (
            "postgres-api-secret",
            "dXNlcjpwYXNz",
            "postgres-private-secret",
        )
        unsafe_body = json.dumps(
            {
                "query": 7,
                "nested": {
                    "api_key": unsafe_values[0],
                    "note": f"Authorization: Basic {unsafe_values[1]}",
                },
                "private_key": unsafe_values[2],
            },
            indent=2,
        )
        request_item = creation.items[0].model_copy(update={
            "inline_body": None if external else unsafe_body,
            "preview_head": "unsafe preview" if external else None,
            "preview_tail": "unsafe preview" if external else None,
            "content_bytes": len(unsafe_body.encode("utf-8")),
        })
        contents = (
            (PersistedTranscriptContentRecord(
                item_id=request_item.item_id,
                body=unsafe_body,
                created_at=request_item.created_at,
            ),)
            if external
            else ()
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(
                runs=creation.runs,
                items=(request_item, creation.items[1]),
                contents=contents,
            ),
        )

        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        reader = TranscriptReader(
            NoLiveTranscript(),
            store,
            create_production_registry(),
            error_id_factory=lambda: UUID(int=1),
            inline_content_bytes=1 if external else 10_000,
            preview_bytes=16,
        )
        try:
            detail = await reader.get_run(run_id, ReadView.HISTORY)
            target = next(
                item for item in detail.items if item.item_id == request_item.item_id
            )
            assert target.parse_status.value == "corrupt"
            assert target.content.kind == "unparsed"
            assert all(
                secret not in detail.model_dump_json()
                for secret in unsafe_values
            )

            content = await reader.get_content(
                run_id,
                request_item.item_id,
                ReadView.HISTORY,
            )
            assert json.loads(content.text) == {
                "query": 7,
                "nested": {
                    "api_key": "[REDACTED]",
                    "note": "Authorization: [REDACTED]",
                },
                "private_key": "[REDACTED]",
            }
            assert content.content_bytes == len(content.text.encode("utf-8"))
            assert content.issues[0].error.code == "transcript_content_corrupt"
            assert all(secret not in content.text for secret in unsafe_values)

            if external:
                valid_body = '{"query":"byte mismatch"}'
                async with engine.begin() as connection:
                    await connection.execute(
                        update(transcript_contents)
                        .where(transcript_contents.c.item_id == request_item.item_id)
                        .values(body=valid_body)
                    )
                    await connection.execute(
                        update(transcript_items)
                        .where(transcript_items.c.item_id == request_item.item_id)
                        .values(content_bytes=1)
                    )
                mismatched = await reader.get_content(
                    run_id,
                    request_item.item_id,
                    ReadView.HISTORY,
                )
                assert json.loads(mismatched.text) == {"query": "byte mismatch"}
                assert mismatched.content_bytes == len(
                    mismatched.text.encode("utf-8")
                )
                assert mismatched.issues[0].error.code == (
                    "transcript_content_corrupt"
                )

            async with engine.begin() as connection:
                if external:
                    await connection.execute(
                        update(transcript_contents)
                        .where(transcript_contents.c.item_id == request_item.item_id)
                        .values(body="{")
                    )
                else:
                    await connection.execute(
                        update(transcript_items)
                        .where(transcript_items.c.item_id == request_item.item_id)
                        .values(inline_body="{", content_bytes=1)
                    )
            with pytest.raises(TranscriptContentUnreadableError):
                await reader.get_content(
                    run_id,
                    request_item.item_id,
                    ReadView.HISTORY,
                )

            if external:
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(transcript_contents).where(
                            transcript_contents.c.item_id == request_item.item_id
                        )
                    )
                with pytest.raises(TranscriptContentUnreadableError):
                    await reader.get_content(
                        run_id,
                        request_item.item_id,
                        ReadView.HISTORY,
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_postgres_history_queries_use_ordered_consistent_read_snapshots(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        created_at = datetime.now(UTC) + timedelta(days=36500)
        prepared = []
        for _ in range(2):
            run_id, batch = _run_batch()
            prepared.append((run_id, PersistenceBatch(
                runs=(batch.runs[0].model_copy(update={
                    "created_at": created_at,
                    "updated_at": created_at,
                }),),
                items=tuple(
                    item.model_copy(update={"created_at": created_at})
                    for item in batch.items
                ),
            )))
        for _, batch in prepared:
            await _commit(postgres_settings, batch)

        run_id = prepared[0][0]
        session_id = uuid4()
        created_item_id = uuid4()
        external_item_id = uuid4()
        external_body = '{"message":"完整正文"}'
        await _commit(
            postgres_settings,
            PersistenceBatch(
                sessions=(PersistedSessionRecord(
                    session_id=session_id,
                    run_id=run_id,
                    agent_role="query_parser",
                    status="pending",
                    created_at=created_at,
                    updated_at=created_at,
                    created_item_id=created_item_id,
                ),),
                items=(
                    _item(
                        run_id=run_id,
                        session_id=session_id,
                        item_id=created_item_id,
                        sequence=3,
                        content_type="lifecycle_event",
                    ).model_copy(update={"created_at": created_at}),
                    PersistedTranscriptItemRecord(
                        item_id=external_item_id,
                        run_id=run_id,
                        session_id=session_id,
                        transcript_sequence=4,
                        created_at=created_at,
                        content_type="fixture_message",
                        content_schema_version=1,
                        preview_head="head",
                        preview_tail="tail",
                        content_bytes=len(external_body.encode("utf-8")),
                    ),
                ),
                contents=(PersistedTranscriptContentRecord(
                    item_id=external_item_id,
                    body=external_body,
                    created_at=created_at,
                ),),
            ),
        )

        engine = create_database_engine(postgres_settings)
        try:
            store = PostgresTranscriptStore(engine, operation_timeout_seconds=5)
            page = await store.list_runs(2, None, None)
            expected_ids = sorted((item[0] for item in prepared), reverse=True)
            assert [entry.run.run_id for entry in page.runs] == expected_ids
            next_page = await store.list_runs(
                1,
                created_at,
                expected_ids[0],
            )
            assert next_page.runs[0].run.run_id == expected_ids[1]

            read = await store.read_run(run_id)
            assert read is not None
            assert read.run.run_id == run_id
            assert [session.session_id for session in read.sessions] == [session_id]
            assert [item.transcript_sequence for item in read.items] == [1, 2, 3, 4]
            assert read.contents[0].item_id == external_item_id
            assert read.contents[0].body == external_body

            async with store._read_transaction() as connection:
                isolation = await connection.scalar(text("SHOW transaction_isolation"))
                read_only = await connection.scalar(text("SHOW transaction_read_only"))
            assert isolation == "repeatable read"
            assert read_only == "on"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_lifespan_repairs_real_postgres_without_restarting_workflow() -> None:
    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[tuple[WorkflowContext, asyncio.Event]] = (
                asyncio.Queue()
            )

        async def run(self, context: WorkflowContext) -> None:
            release = asyncio.Event()
            self.calls.put_nowait((context, release))
            await release.wait()
            context.cancellation.checkpoint()

    async def exercise(settings: AppSettings) -> None:
        workflow = Workflow()
        first_app = create_app(
            settings,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        async with first_app.router.lifespan_context(first_app):
            coordinator = first_app.state.run_coordinator
            interrupted = await coordinator.create_run(
                b'{"query":"real PG interrupted"}'
            )
            await workflow.calls.get()
            interrupted_session = await coordinator.prepare_agent_session(
                interrupted.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                interrupted.run_id,
                interrupted_session.session_id,
            )
            await _wait_persistence(coordinator, interrupted.run_id)

            terminal_parent = await coordinator.create_run(
                b'{"query":"terminal parent"}'
            )
            await workflow.calls.get()
            terminal_child = await coordinator.prepare_agent_session(
                terminal_parent.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="44001",
            )
            await coordinator.mark_agent_session_running(
                terminal_parent.run_id,
                terminal_child.session_id,
            )
            await coordinator.complete_run(terminal_parent.run_id)
            await _wait_persistence(coordinator, terminal_parent.run_id)

            first_store = first_app.state.transcript_store
            before_interrupted = await first_store.read_run(interrupted.run_id)
            before_terminal = await first_store.read_run(terminal_parent.run_id)
            assert before_interrupted is not None and before_terminal is not None

        second_app = create_app(settings, shutdown_grace_period=0)
        async with second_app.router.lifespan_context(second_app):
            store = second_app.state.transcript_store
            repaired = await store.read_run(interrupted.run_id)
            child_repaired = await store.read_run(terminal_parent.run_id)
            assert repaired is not None and child_repaired is not None
            assert repaired.run.status is RunStatus.FAILED
            assert repaired.sessions[0].status is AgentSessionStatus.FAILED
            assert child_repaired.run.status is RunStatus.COMPLETED
            assert child_repaired.sessions[0].status is AgentSessionStatus.FAILED
            assert repaired.run.created_at == before_interrupted.run.created_at
            assert child_repaired.run.updated_at == before_terminal.run.updated_at
            assert [item.transcript_sequence for item in repaired.items] == list(
                range(1, len(repaired.items) + 1)
            )
            repaired_counts = (
                len(repaired.items),
                len(child_repaired.items),
            )

        third_app = create_app(settings, shutdown_grace_period=0)
        async with third_app.router.lifespan_context(third_app):
            store = third_app.state.transcript_store
            repeated = await store.read_run(interrupted.run_id)
            repeated_child = await store.read_run(terminal_parent.run_id)
            assert repeated is not None and repeated_child is not None
            assert (len(repeated.items), len(repeated_child.items)) == repaired_counts

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


async def _count_items(engine: AsyncEngine, item_ids: tuple[UUID, ...]) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(
            select(text("count(*)"))
            .select_from(transcript_items)
            .where(transcript_items.c.item_id.in_(item_ids))
        )
        return int(result or 0)
