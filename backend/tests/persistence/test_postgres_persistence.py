import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import delete, event, inspect, select, text, update
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
from app.application.transcripts.models import WorkflowQuiescedFact
from app.application.transcripts.read_models import (
    ReadView,
    RunDetailResponse,
    SessionDetailResponse,
)
from app.application.transcripts.reader import (
    TranscriptContentUnreadableError,
    TranscriptReader,
)
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.persistence.database import (
    check_database_schema,
    create_database_engine,
    upgrade_database,
)
from app.infrastructure.persistence.records import (
    PersistedRunRecord,
    PersistedSessionRecord,
    PersistedTranscriptContentRecord,
    PersistedTranscriptItemRecord,
    PersistenceBatch,
)
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    metadata,
    transcript_contents,
    transcript_items,
)
from app.infrastructure.persistence.store import PostgresTranscriptStore
from app.infrastructure.runtime.system_clock import SystemClock
from scripts.test_database import temporary_migrated_schema
from tests.application.evidence_tools.fakes import FixedClock
from tests.application.reasoning.test_planning_loop import _fixture
from tests.application.reasoning.test_runner import (
    AdaptiveWaterbodyModelPort,
    _runner,
)
from tests.m4_support import (
    MODEL_CATALOG,
    analysis_context,
    create_request,
    create_request_bytes,
)
from tests.m4_support import (
    create_test_app as create_app,
)


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
                lambda: coordinator._transcript_writer.info_locked(runtime).state
                is not PersistenceState.PENDING
            )
            info = coordinator._transcript_writer.info_locked(runtime)
    if info.state is not PersistenceState.UP_TO_DATE:
        raise AssertionError(info)


def test_read_run_items_selects_only_requested_persisted_content(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, batch = _run_batch()
        await _commit(postgres_settings, batch)
        external_id = uuid4()
        now = datetime.now(UTC)
        body = '{"message":"selected"}'
        await _commit(postgres_settings, PersistenceBatch(
            items=(PersistedTranscriptItemRecord(
                item_id=external_id,
                run_id=run_id,
                transcript_sequence=3,
                created_at=now,
                content_type="fixture_message",
                content_schema_version=1,
                preview_head="head",
                preview_tail="tail",
                content_bytes=len(body.encode()),
            ),),
            contents=(PersistedTranscriptContentRecord(
                item_id=external_id,
                body=body,
                created_at=now,
            ),),
        ))
        engine = create_database_engine(postgres_settings)
        try:
            store = PostgresTranscriptStore(
                engine,
                operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
            )
            selected = await store.read_run_items(
                run_id, (external_id, batch.items[0].item_id)
            )
            assert selected is not None
            assert tuple(item.item_id for item in selected.items) == (
                batch.items[0].item_id, external_id,
            )
            assert tuple(content.item_id for content in selected.contents) == (
                external_id,
            )
            assert selected.contents[0].body == body
            assert await store.read_run_items(uuid4(), (external_id,)) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_history_content_and_session_reads_skip_unrelated_external_body(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        await _commit(postgres_settings, creation)
        session_id, other_session_id = uuid4(), uuid4()
        created_id, other_created_id = uuid4(), uuid4()
        requested_id, unrelated_id = uuid4(), uuid4()
        now = datetime.now(UTC)
        requested_body = create_production_registry().encode(
            "runtime_fact", 1, WorkflowQuiescedFact()
        ).safe_body
        unrelated_body = json.dumps({"blob": "x" * 100_000})

        def created_body(item_session_id: UUID) -> str:
            return json.dumps({
                "schema_version": 2,
                "run_id": str(run_id),
                "session_id": str(item_session_id),
                "sequence": 1,
                "occurred_at": now.isoformat(),
                "type": "session_created",
                "payload": {"status": "pending", "created_at": now.isoformat()},
            })

        await _commit(postgres_settings, PersistenceBatch(
            sessions=(
                PersistedSessionRecord(
                    session_id=session_id, run_id=run_id,
                    agent_role="waterbody", target_comid="12345678",
                    status="pending", created_at=now, updated_at=now,
                    created_item_id=created_id,
                ),
                PersistedSessionRecord(
                    session_id=other_session_id, run_id=run_id,
                    agent_role="waterbody", target_comid="87654321",
                    status="pending", created_at=now, updated_at=now,
                    created_item_id=other_created_id,
                ),
            ),
            items=(
                _item(
                    run_id=run_id, session_id=session_id,
                    item_id=created_id, sequence=3,
                    body=created_body(session_id), content_type="lifecycle_event",
                ).model_copy(update={"content_schema_version": 2}),
                PersistedTranscriptItemRecord(
                    item_id=requested_id, run_id=run_id, session_id=session_id,
                    transcript_sequence=4, created_at=now,
                    content_type="runtime_fact", content_schema_version=1,
                    preview_head="head", preview_tail="tail",
                    content_bytes=len(requested_body.encode()),
                ),
                _item(
                    run_id=run_id, session_id=other_session_id,
                    item_id=other_created_id, sequence=5,
                    body=created_body(other_session_id),
                    content_type="lifecycle_event",
                ).model_copy(update={"content_schema_version": 2}),
                PersistedTranscriptItemRecord(
                    item_id=unrelated_id, run_id=run_id,
                    session_id=other_session_id,
                    transcript_sequence=6, created_at=now,
                    content_type="fixture_message", content_schema_version=1,
                    preview_head="head", preview_tail="tail",
                    content_bytes=len(unrelated_body.encode()),
                ),
            ),
            contents=(
                PersistedTranscriptContentRecord(
                    item_id=requested_id, body=requested_body, created_at=now,
                ),
                PersistedTranscriptContentRecord(
                    item_id=unrelated_id, body=unrelated_body, created_at=now,
                ),
            ),
        ))

        engine = create_database_engine(postgres_settings)
        content_select_parameters: list[object] = []

        def capture_content_select(_conn, _cursor, statement, parameters, _context, _many):
            if "transcript_contents" in statement and "SELECT" in statement:
                content_select_parameters.append(parameters)

        event.listen(engine.sync_engine, "before_cursor_execute", capture_content_select)
        try:
            class BoundedStore(PostgresTranscriptStore):
                async def read_run(self, run_id):
                    raise AssertionError(f"full-run read attempted for {run_id}")

            reader = TranscriptReader(
                NoLiveTranscript(),
                BoundedStore(
                    engine,
                    operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
                ),
                create_production_registry(),
                error_id_factory=uuid4,
                inline_content_bytes=256,
                preview_bytes=16,
            )
            content = await reader.get_content(run_id, requested_id, ReadView.HISTORY)
            assert content.item_id == requested_id
            session = await reader.get_session(run_id, session_id, ReadView.HISTORY)
            assert session.session.session_id == session_id
            transcript = await reader.get_session_transcript(
                run_id, session_id, ReadView.HISTORY
            )
            assert {item.item_id for item in transcript.items} == {
                created_id, requested_id,
            }
            assert content_select_parameters
            assert any(
                str(requested_id) in str(params)
                for params in content_select_parameters
            )
            assert all(
                str(unrelated_id) not in str(params)
                for params in content_select_parameters
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_content_select)
            await engine.dispose()

    asyncio.run(exercise())


def test_list_session_item_ids_selects_only_requested_type(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, batch = _run_batch()
        await _commit(postgres_settings, batch)
        session_id = uuid4()
        created_id, first_bundle_id, fragment_id, last_bundle_id = (
            uuid4() for _ in range(4)
        )
        now = datetime.now(UTC)
        await _commit(postgres_settings, PersistenceBatch(
            sessions=(PersistedSessionRecord(
                session_id=session_id,
                run_id=run_id,
                agent_role="waterbody",
                target_comid="12345",
                status="pending",
                created_at=now,
                updated_at=now,
                created_item_id=created_id,
            ),),
            items=(
                _item(run_id=run_id, session_id=session_id, item_id=created_id,
                      sequence=3, content_type="lifecycle_event"),
                _item(run_id=run_id, session_id=session_id, item_id=first_bundle_id,
                      sequence=4, content_type="evidence_bundle"),
                _item(run_id=run_id, session_id=session_id, item_id=fragment_id,
                      sequence=5, content_type="evidence_fragment"),
                _item(run_id=run_id, session_id=session_id, item_id=last_bundle_id,
                      sequence=6, content_type="evidence_bundle"),
            ),
        ))
        engine = create_database_engine(postgres_settings)
        try:
            store = PostgresTranscriptStore(
                engine,
                operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
            )
            assert await store.run_exists(run_id)
            assert not await store.run_exists(uuid4())
            assert await store.session_exists(run_id, session_id)
            assert not await store.session_exists(run_id, uuid4())
            assert not await store.session_exists(uuid4(), session_id)
            assert await store.list_session_item_ids(
                run_id, session_id, "evidence_bundle"
            ) == (first_bundle_id, last_bundle_id)
            assert await store.list_session_item_ids(
                run_id, session_id, None
            ) == (created_id, first_bundle_id, fragment_id, last_bundle_id)
            assert await store.list_session_item_ids(
                run_id, uuid4(), "evidence_bundle"
            ) == ()
            assert await store.list_session_item_ids(
                uuid4(), session_id, "evidence_bundle"
            ) == ()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_upgrade_check_and_repeated_upgrade_on_real_postgres(
    postgres_settings: AppSettings,
) -> None:
    assert asyncio.run(check_database_schema(postgres_settings)) == "20261020_02"
    assert upgrade_database(postgres_settings) == "20261020_02"

    async def inspect_schema() -> tuple[set[str], set[str], list[object]]:
        engine = create_database_engine(postgres_settings)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config("
                        "'search_path', current_schema() || ',public', false)"
                    )
                )
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
                            MigrationContext.configure(
                                sync_connection,
                                opts={
                                    "compare_type": lambda _context, inspected,
                                    metadata_type, *_args: (
                                        False
                                        if str(metadata_type).casefold() == "vector"
                                        and str(inspected).casefold()
                                        in {"null", "public.vector"}
                                        else None
                                    )
                                },
                            ),
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
        "rag_index_generations",
        "rag_index_jobs",
        "rag_case_projections",
        "rag_case_windows",
        "rag_fragment_projections",
        "rag_consumption_links",
        "runtime_settings",
        "unit_policy_versions",
        "unit_policy_entries",
        "unit_policy_confirmations",
        "analysis_run_idempotency_tombstones",
        "admin_operations",
        "guideline_documents",
        "guideline_versions",
        "guideline_catalog_state",
        "guideline_catalog_entries",
        "guideline_index_generations",
        "guideline_embeddings",
        "guideline_operations",
    }
    assert "ck_analysis_runs_terminal_item_matches_status" in constraints
    assert "ck_analysis_sessions_role_comid" in constraints
    assert "ck_transcript_items_inline_or_preview" in constraints
    assert schema_diff == [], repr(schema_diff)


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


def test_database_allows_exactly_one_run_summarizer_without_comid(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, run_batch = _run_batch()
        now = datetime.now(UTC)
        session_id = uuid4()
        created_item_id = uuid4()
        summarizer = PersistedSessionRecord(
            session_id=session_id,
            run_id=run_id,
            agent_role="run_summarizer",
            status="pending",
            created_at=now,
            updated_at=now,
            created_item_id=created_item_id,
        )
        await _commit(
            postgres_settings,
            PersistenceBatch(
                runs=run_batch.runs,
                sessions=(summarizer,),
                items=(
                    *run_batch.items,
                    _item(
                        run_id=run_id,
                        session_id=session_id,
                        item_id=created_item_id,
                        sequence=3,
                        content_type="lifecycle_event",
                    ),
                ),
            ),
        )

        duplicate_session_id = uuid4()
        duplicate_created_item_id = uuid4()
        with pytest.raises(IntegrityError):
            await _commit(
                postgres_settings,
                PersistenceBatch(
                    sessions=(
                        summarizer.model_copy(
                            update={
                                "session_id": duplicate_session_id,
                                "created_item_id": duplicate_created_item_id,
                            }
                        ),
                    ),
                    items=(
                        _item(
                            run_id=run_id,
                            session_id=duplicate_session_id,
                            item_id=duplicate_created_item_id,
                            sequence=4,
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
                model_catalog=MODEL_CATALOG,
                transcript_store=store,
                workflow=workflow,
                shutdown_grace_period=0,
            )
            for _ in range(2)
        ]
        key = uuid4()
        body = create_request_bytes("database race")
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
                    b" " + body,
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
            model_catalog=MODEL_CATALOG,
            transcript_store=store,
            workflow=workflow,
            record_id_factory=record_ids.__next__,
            inline_content_bytes=1,
            preview_bytes=1,
            shutdown_grace_period=0,
        )
        key = uuid4()
        body = create_request_bytes("external creation content")
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
            model_catalog=MODEL_CATALOG,
            transcript_store=store,
            workflow=workflow,
            persistence_retry_delays=(0, 0, 0, 0, 0),
            shutdown_grace_period=0,
            shutdown_save_grace_period=1,
        )
        try:
            created = await coordinator.create_run(
                create_request_bytes("writer integration")
            )
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
                create_request("水" * 30000),
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
            model_catalog=MODEL_CATALOG,
            transcript_store=store,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        try:
            created = await coordinator.create_run(
                create_request_bytes("session verify")
            )
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
            model_catalog=MODEL_CATALOG,
            transcript_store=store,
            workflow=workflow,
            shutdown_grace_period=0,
            shutdown_save_grace_period=0,
            persistence_retry_delays=(0, 0, 0, 0, 0),
            transcript_registry=registry,
        )
        try:
            created = await coordinator.create_run(
                create_request_bytes("referenced cancellation")
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
                create_request("late finalize must stay suppressed"),
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
def test_real_postgres_invalid_schema_preserves_stored_fulltext_when_readable(
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
            assert content.text == unsafe_body
            assert json.loads(content.text) == {
                "query": 7,
                "nested": {
                    "api_key": unsafe_values[0],
                    "note": f"Authorization: Basic {unsafe_values[1]}",
                },
                "private_key": unsafe_values[2],
            }
            assert content.content_bytes == len(content.text.encode("utf-8"))
            assert content.issues[0].error.code == "transcript_content_corrupt"

            if external:
                valid_body = create_request_bytes("byte mismatch").decode()
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
                assert json.loads(mismatched.text) == create_request("byte mismatch")
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


def test_corrupt_completed_session_terminal_keeps_postgres_history_readable(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        neighbor_run_id, neighbor_creation = _run_batch()
        request_body = create_request_bytes("valid neighbor").decode()
        request_item = creation.items[0].model_copy(update={
            "inline_body": request_body,
            "content_bytes": len(request_body.encode("utf-8")),
        })
        await _commit(postgres_settings, PersistenceBatch(
            runs=creation.runs,
            items=(request_item, creation.items[1]),
        ))
        await _commit(postgres_settings, neighbor_creation)

        session_id = uuid4()
        created_id = uuid4()
        terminal_id = uuid4()
        unrelated_id = uuid4()
        now = datetime.now(UTC)
        created_body = json.dumps({
            "schema_version": 2,
            "run_id": str(run_id),
            "session_id": str(session_id),
            "sequence": 1,
            "occurred_at": now.isoformat(),
            "type": "session_created",
            "payload": {"status": "pending", "created_at": now.isoformat()},
        })
        damaged_body = '{"invalid": true}'
        await _commit(postgres_settings, PersistenceBatch(
            sessions=(PersistedSessionRecord(
                session_id=session_id,
                run_id=run_id,
                agent_role="waterbody",
                target_comid="12345678",
                status="completed",
                created_at=now,
                updated_at=now,
                created_item_id=created_id,
                terminal_item_id=terminal_id,
            ),),
            items=(
                _item(
                    run_id=run_id, item_id=created_id, sequence=3,
                    session_id=session_id, body=created_body,
                    content_type="lifecycle_event",
                ).model_copy(update={"content_schema_version": 2}),
                _item(
                    run_id=run_id, item_id=terminal_id, sequence=4,
                    session_id=session_id, body=damaged_body,
                    content_type="lifecycle_event",
                ).model_copy(update={"content_schema_version": 2}),
                _item(
                    run_id=run_id, item_id=unrelated_id, sequence=5,
                    session_id=session_id, body='{"unrelated": true}',
                    content_type="runtime_fact",
                ),
            ),
        ))

        engine = create_database_engine(postgres_settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
        )
        reader = TranscriptReader(
            NoLiveTranscript(), store, create_production_registry(),
            error_id_factory=lambda: UUID(int=1),
            inline_content_bytes=256, preview_bytes=16,
        )
        try:
            page = await store.list_runs(10, None, None)
            assert {item.run.run_id for item in page.runs} >= {
                run_id, neighbor_run_id
            }
            stored = await store.read_run(run_id)
            assert stored is not None
            assert stored.sessions[0].status is AgentSessionStatus.COMPLETED
            assert stored.sessions[0].result_ref is None
            assert stored.session_issues[0].item_id == terminal_id
            roots = await store.read_run_detail_roots(run_id)
            assert roots is not None
            assert roots.session_issues[0].item_id == terminal_id
            assert unrelated_id not in {item.item_id for item in roots.items}
            detail = await reader.get_run(run_id, ReadView.HISTORY)
            assert any(issue.item_id == terminal_id for issue in detail.issues)
            assert not any(issue.item_id == unrelated_id for issue in detail.issues)
            assert any(item.item_id == request_item.item_id for item in detail.items)
            session = await reader.get_session(run_id, session_id, ReadView.HISTORY)
            assert session.session.status is AgentSessionStatus.COMPLETED
            assert session.session.result_ref is None
            assert session.issues[0].error.code == "transcript_content_corrupt"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("damage", ("unsupported_version", "external_length"))
def test_completed_session_does_not_trust_invalid_terminal_metadata(
    postgres_settings: AppSettings, damage: str,
) -> None:
    async def exercise() -> None:
        run_id, creation = _run_batch()
        await _commit(postgres_settings, creation)
        session_id, created_id, terminal_id, result_item_id = (
            uuid4(), uuid4(), uuid4(), uuid4()
        )
        now = datetime.now(UTC)
        created_body = json.dumps({
            "schema_version": 2, "run_id": str(run_id),
            "session_id": str(session_id), "sequence": 1,
            "occurred_at": now.isoformat(), "type": "session_created",
            "payload": {"status": "pending", "created_at": now.isoformat()},
        })
        terminal_body = json.dumps({
            "schema_version": 2, "run_id": str(run_id),
            "session_id": str(session_id), "sequence": 2,
            "occurred_at": now.isoformat(), "type": "session_completed",
            "payload": {
                "status": "completed", "completed_at": now.isoformat(),
                "result_ref": {
                    "run_id": str(run_id), "item_id": str(result_item_id),
                },
                "result_summary": {
                    "result_kind": "waterbody", "result_id": "valid-result",
                    "target_comid": "12345678", "risk_level": "medium",
                    "availability": "complete", "overall_confidence": 0.8,
                },
            },
        })
        created_item = _item(
            run_id=run_id, item_id=created_id, sequence=3,
            session_id=session_id, body=created_body,
            content_type="lifecycle_event",
        ).model_copy(update={"content_schema_version": 2})
        terminal_item = _item(
            run_id=run_id, item_id=terminal_id, sequence=4,
            session_id=session_id, body=terminal_body,
            content_type="lifecycle_event",
        ).model_copy(update={"content_schema_version": 2})
        contents = ()
        if damage == "external_length":
            terminal_item = terminal_item.model_copy(update={
                "inline_body": None, "preview_head": "head",
                "preview_tail": "tail",
            })
            contents = (PersistedTranscriptContentRecord(
                item_id=terminal_id, body=terminal_body,
                created_at=terminal_item.created_at,
            ),)
        await _commit(postgres_settings, PersistenceBatch(
            sessions=(PersistedSessionRecord(
                session_id=session_id, run_id=run_id,
                agent_role="waterbody", target_comid="12345678",
                status="completed", created_at=now, updated_at=now,
                created_item_id=created_id, terminal_item_id=terminal_id,
            ),),
            items=(created_item, terminal_item), contents=contents,
        ))
        engine = create_database_engine(postgres_settings)
        try:
            async with engine.begin() as connection:
                if damage == "unsupported_version":
                    await connection.execute(
                        update(transcript_items)
                        .where(transcript_items.c.item_id == terminal_id)
                        .values(content_schema_version=999)
                    )
                else:
                    await connection.execute(
                        update(transcript_contents)
                        .where(transcript_contents.c.item_id == terminal_id)
                        .values(body=terminal_body + " ")
                    )
            store = PostgresTranscriptStore(
                engine,
                operation_timeout_seconds=postgres_settings.db_operation_timeout_seconds,
            )
            reader = TranscriptReader(
                NoLiveTranscript(), store, create_production_registry(),
                error_id_factory=lambda: UUID(int=1),
                inline_content_bytes=256, preview_bytes=16,
            )
            listed = await store.list_runs(10, None, None)
            assert any(item.run.run_id == run_id for item in listed.runs)
            detail = await reader.get_session(run_id, session_id, ReadView.HISTORY)
            assert detail.session.result_ref is None
            assert detail.session.result_summary is None
            assert detail.session.terminal_corrupt is True
            expected = (
                "transcript_content_unsupported"
                if damage == "unsupported_version"
                else "transcript_content_corrupt"
            )
            assert any(issue.error.code == expected for issue in detail.issues)
            assert SessionDetailResponse.model_validate_json(detail.model_dump_json()) == detail
            run_detail = await reader.get_run(run_id, ReadView.HISTORY)
            assert RunDetailResponse.model_validate_json(run_detail.model_dump_json()) == run_detail
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
                create_request_bytes("real PG interrupted")
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
                create_request_bytes("terminal parent")
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


def test_m4_context_history_projection_survives_real_postgres_restart() -> None:
    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise(base_settings: AppSettings) -> None:
        settings = base_settings.model_copy(update={
            "transcript_inline_max_bytes": 256,
            "transcript_preview_bytes": 40,
        })
        workflow = Workflow()
        first_app = create_app(
            settings,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        async with first_app.router.lifespan_context(first_app):
            coordinator = first_app.state.run_coordinator
            query = "Will dissolved oxygen become unsafe?"
            created = await coordinator.create_run(create_request_bytes(query))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            context_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                context_item.item_id,
                "query_understood",
                1,
                analysis_context(query),
            )
            await coordinator.complete_agent_session(
                created.run_id,
                session.session_id,
            )
            workflow.release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            await _wait_persistence(coordinator, created.run_id)

        second_app = create_app(settings, shutdown_grace_period=0)
        async with second_app.router.lifespan_context(second_app):
            reader = second_app.state.transcript_reader
            detail = await reader.get_run(created.run_id, ReadView.HISTORY)
            assert detail.run.context_summary is not None
            assert detail.run.context_summary.target_comid == "12345678"
            assert detail.run.context_summary.params_used[0].source.value == "explicit"
            assert detail.run.context_summary.params_used[0].rationale == (
                "Dissolved oxygen is explicitly requested."
            )
            context_view = next(
                item
                for item in detail.referenced_items
                if item.content_type == "query_understood"
            )
            assert context_view.scope.agent_role is AgentRole.QUERY_PARSER
            assert context_view.content.kind == "reference"
            history = await reader.list_runs(limit=200, cursor=None)
            listed = next(
                item for item in history.items if item.run_id == created.run_id
            )
            assert listed.context_summary == detail.run.context_summary

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_m9_waterbody_result_projection_survives_real_postgres_restart() -> None:
    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise(postgres_settings: AppSettings) -> None:
        model_port = AdaptiveWaterbodyModelPort()
        _scope, _evidence_store, loop, task = await _fixture(
            model_port,
            water_mode="full",
        )
        outcome = await _runner(loop, model_port).run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert outcome.result is not None

        settings = postgres_settings.model_copy(update={
            "transcript_inline_max_bytes": 256,
            "transcript_preview_bytes": 40,
        })
        workflow = Workflow()
        first_app = create_app(
            settings,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        async with first_app.router.lifespan_context(first_app):
            coordinator = first_app.state.run_coordinator
            created = await coordinator.create_run(create_request_bytes("M9 result"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="1234567",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            result = outcome.result.model_copy(update={
                "run_id": created.run_id,
                "session_id": session.session_id,
                "target_comid": "1234567",
                "analysis_detail": outcome.result.analysis_detail.model_copy(
                    update={"transcript_item_refs": ()}
                ),
            })
            completed = await coordinator.complete_waterbody_agent_session(
                created.run_id,
                session.session_id,
                result,
            )
            workflow.release.set()
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            await _wait_persistence(coordinator, created.run_id)

        second_app = create_app(settings, shutdown_grace_period=0)
        async with second_app.router.lifespan_context(second_app):
            reader = second_app.state.transcript_reader
            session_detail = await reader.get_session(
                created.run_id,
                session.session_id,
                ReadView.HISTORY,
            )
            assert session_detail.session.result_ref == completed.result_ref
            assert session_detail.session.result_summary == completed.result_summary

            run_detail = await reader.get_run(created.run_id, ReadView.HISTORY)
            restored = next(
                item
                for item in run_detail.sessions
                if item.session_id == session.session_id
            )
            assert restored.result_ref == completed.result_ref
            assert restored.result_summary == completed.result_summary

            stored_history = await second_app.state.transcript_store.list_runs(
                200,
                None,
                None,
            )
            listed = next(
                item
                for item in stored_history.runs
                if item.run.run_id == created.run_id
            )
            listed_session = next(
                item
                for item in listed.sessions
                if item.session_id == session.session_id
            )
            assert listed_session.result_ref == completed.result_ref
            assert listed_session.result_summary == completed.result_summary

            transcript = await reader.get_session_transcript(
                created.run_id,
                session.session_id,
                ReadView.HISTORY,
            )
            assert tuple(item.content_type for item in transcript.items[-2:]) == (
                "inference_result",
                "lifecycle_event",
            )
            result_item = transcript.items[-2]
            assert result_item.content.kind == "reference"
            full_result = await reader.get_content(
                created.run_id,
                result_item.item_id,
                ReadView.HISTORY,
            )
            assert result.result_id in full_result.text

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_m5_typed_content_reloads_from_postgres_and_startup_repair_keeps_closure(
) -> None:
    from app.domain import (
        CollectionCompletionProof,
        CollectionCounts,
        CollectionFactPayload,
        CollectionManifest,
        CollectionPageManifest,
        CollectionStatus,
        CollectionSummary,
        CoverageStatus,
        EvidenceApplicability,
        EvidenceBundle,
        EvidenceCell,
        EvidenceCellRole,
        EvidenceCellStatus,
        EvidenceCoverage,
        EvidenceFragment,
        EvidenceFragmentReference,
        EvidenceLocation,
        EvidenceResultEligibility,
        EvidenceSourceRecords,
        EvidenceToolCall,
        EvidenceToolCounts,
        EvidenceToolExecutionStatus,
        EvidenceToolResult,
        ItemReference,
        Provenance,
        QualitySummary,
        WindowCompleteness,
    )
    from tests.application.evidence_tools.conftest import make_context, make_graph

    class Workflow:
        def __init__(self) -> None:
            self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
            self.release = asyncio.Event()

        async def run(self, context: WorkflowContext) -> None:
            self.calls.put_nowait(context)
            await self.release.wait()
            context.cancellation.checkpoint()

    async def exercise(postgres_settings: AppSettings) -> None:
        workflow = Workflow()
        settings = postgres_settings.model_copy(update={
            "transcript_inline_max_bytes": 256,
            "transcript_preview_bytes": 40,
        })
        first_app = create_app(
            settings,
            workflow=workflow,
            shutdown_grace_period=0,
        )
        async with first_app.router.lifespan_context(first_app):
            coordinator = first_app.state.run_coordinator
            created = await coordinator.create_run(create_request_bytes("M5 evidence"))
            await workflow.calls.get()
            parser = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                parser.session_id,
            )
            context = make_context()
            context_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=parser.session_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                context_item.item_id,
                "query_understood",
                1,
                context,
            )
            graph = make_graph()
            topology_item = await coordinator.reserve_transcript_item(created.run_id)
            await coordinator.finalize_transcript_item(
                created.run_id,
                topology_item.item_id,
                "topology_ready",
                1,
                graph,
            )
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="123",
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            operation_id = str(uuid4())
            call_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
                operation_id=operation_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                call_item.item_id,
                "evidence_tool_call",
                1,
                EvidenceToolCall(
                    tool_call_id="call-pg-1",
                    session_id=str(session.session_id),
                    tool_name="collect_water_observations",
                    purpose="local_station_preflight",
                    window_ids=("window-000",),
                    parameter_keys=(
                        context.params_used[0].key,
                        context.supporting_params[0].key,
                    ),
                    policy="conditional_mandatory",
                    requested_at=call_item.created_at,
                ),
            )
            window = context.time_segments[0].time_range
            manifest = CollectionManifest(
                manifest_id="manifest-pg-1",
                operation_id=operation_id,
                provider="wdfn_time_series",
                channel="continuous",
                collection_status=CollectionStatus.EMPTY,
                request_range=window,
                requested_series=("series-pg-1",),
                page_or_chunk_manifests=(CollectionPageManifest(
                    page_identity="page-pg-1",
                    record_count=0,
                    response_bytes=1,
                    page_complete=True,
                ),),
                completed_ranges=(window,),
                affected_window_ids=("window-000",),
                window_completeness=(WindowCompleteness(
                    window_id="window-000",
                    complete=True,
                    status=CollectionStatus.EMPTY,
                ),),
                counts=CollectionCounts(
                    requests=1,
                    pages=1,
                    records=0,
                    response_bytes=1,
                ),
                completion_proof=CollectionCompletionProof(
                    contract="single_response",
                    complete=True,
                    terminal_identity="page-pg-1",
                    digest="sha256:pg-proof",
                ),
            )
            source_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
                operation_id=operation_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                source_item.item_id,
                "evidence_source_records",
                1,
                EvidenceSourceRecords(
                    operation_id=operation_id,
                    batch_id=manifest.manifest_id,
                    manifest=manifest,
                ),
            )
            fragment = EvidenceFragment(
                evidence_id="evidence-pg-empty",
                source="wdfn_time_series",
                collection_status=CollectionStatus.EMPTY,
                target_comid="123",
                location=EvidenceLocation(
                    point=None,
                    comid="123",
                    station_id="USGS-123",
                    distance_to_target_km=0,
                ),
                time_range=window,
                parameter_refs=(
                    context.params_used[0].key,
                    context.supporting_params[0].key,
                ),
                data_type="collection_fact",
                applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
                series_identity="sha256:pg-series",
                quality_class=None,
                coverage=EvidenceCoverage(
                    status=CoverageStatus.INSUFFICIENT,
                    observed_count=0,
                    expected_count=None,
                    ratio=None,
                    covered_duration_seconds=0,
                    window_duration_seconds=int(
                        (window.end - window.start).total_seconds()
                    ),
                    largest_gap_seconds=None,
                ),
                payload=CollectionFactPayload(
                    status=CollectionStatus.EMPTY,
                    reason="no_records_in_completed_query",
                    affected_window_ids=("window-000",),
                ),
                provenance=Provenance(operation_id=operation_id),
                result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
            )
            fragment_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
                operation_id=operation_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                fragment_item.item_id,
                "evidence_fragment",
                1,
                fragment,
            )
            fragment_ref = EvidenceFragmentReference(
                item_id=str(fragment_item.item_id),
                evidence_id=fragment.evidence_id,
            )
            result = EvidenceToolResult(
                tool_call_id="call-pg-1",
                operation_id=operation_id,
                tool_name="collect_water_observations",
                execution_status=EvidenceToolExecutionStatus.COMPLETED,
                collection_status=CollectionStatus.EMPTY,
                manifest_refs=(ItemReference(item_id=str(source_item.item_id)),),
                collection_fact_refs=(fragment_ref,),
                counts=EvidenceToolCounts(requests=1, pages=1, fragments=1),
                completed_at=datetime.now(UTC),
            )
            result_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
                operation_id=operation_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                result_item.item_id,
                "evidence_tool_result",
                1,
                result,
            )
            cells = tuple(
                EvidenceCell(
                    parameter=parameter,
                    window_id="window-000",
                    role=role,
                    collection_fact_refs=(fragment.evidence_id,),
                    status=EvidenceCellStatus.UNSUPPORTED,
                    reasons=("no_usable_evidence",),
                )
                for parameter, role in (
                    (context.params_used[0].key, EvidenceCellRole.TARGET),
                    (
                        context.supporting_params[0].key,
                        EvidenceCellRole.REQUIRED_SUPPORT,
                    ),
                )
            )
            bundle = EvidenceBundle(
                bundle_id="bundle-pg-1",
                agent_comid="123",
                context_ref=ItemReference(item_id=str(context_item.item_id)),
                topology_ref=ItemReference(item_id=str(topology_item.item_id)),
                time_range=window,
                analysis_timezone=context.analysis_timezone,
                time_resolution=context.time_resolution,
                time_resolution_source=context.time_resolution_source,
                requested_parameters=(context.params_used[0].key,),
                supporting_parameters=(context.supporting_params[0].key,),
                window_refs=("window-000",),
                cells=cells,
                fragment_refs=(fragment_ref,),
                source_record_item_refs=(str(source_item.item_id),),
                tool_result_refs=(ItemReference(item_id=str(result_item.item_id)),),
                quality_summary=QualitySummary(),
                collection_summary=CollectionSummary(empty=1),
                created_at=datetime.now(UTC),
            )
            bundle_item = await coordinator.reserve_transcript_item(
                created.run_id,
                session_id=session.session_id,
            )
            await coordinator.finalize_transcript_item(
                created.run_id,
                bundle_item.item_id,
                "evidence_bundle",
                1,
                bundle,
            )
            await _wait_persistence(coordinator, created.run_id)
            workflow.release.set()

        second_app = create_app(settings, shutdown_grace_period=0)
        async with second_app.router.lifespan_context(second_app):
            history = await second_app.state.transcript_reader.get_session_transcript(
                created.run_id,
                session.session_id,
                ReadView.HISTORY,
            )
            evidence = tuple(
                item
                for item in history.items
                if item.content_type.startswith("evidence_")
            )
            assert tuple(item.content_type for item in evidence) == (
                "evidence_tool_call",
                "evidence_source_records",
                "evidence_fragment",
                "evidence_tool_result",
                "evidence_bundle",
            )
            assert all(item.parse_status.value == "ok" for item in evidence)
            assert not any(
                issue.error.code == "transcript_reference_unreadable"
                for issue in history.issues
            )

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
