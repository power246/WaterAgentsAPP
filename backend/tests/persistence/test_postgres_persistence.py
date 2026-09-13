import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

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
from app.infrastructure.persistence.store import PostgresTranscriptStore


@pytest.fixture(scope="module")
def postgres_settings() -> AppSettings:
    port = os.environ.get("WATERAGENTS_TEST_POSTGRES_PORT")
    if port is None:
        pytest.skip("set WATERAGENTS_TEST_POSTGRES_PORT for real PostgreSQL tests")
    return AppSettings(
        _env_file=None,
        db_host=os.environ.get("WATERAGENTS_TEST_POSTGRES_HOST", "127.0.0.1"),
        db_port=int(port),
        db_name=os.environ.get("WATERAGENTS_TEST_POSTGRES_DB", "wateragents_m2_pytest"),
        db_user=os.environ.get("WATERAGENTS_TEST_POSTGRES_USER", "wateragents"),
        db_password=os.environ.get("WATERAGENTS_TEST_POSTGRES_PASSWORD", "test-only"),
        db_sslmode="disable",
    )


@pytest.fixture(scope="module", autouse=True)
def migrated_postgres(postgres_settings: AppSettings) -> None:
    assert upgrade_database(postgres_settings) == "20260913_01"


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


async def _count_items(engine: AsyncEngine, item_ids: tuple[UUID, ...]) -> int:
    async with engine.connect() as connection:
        result = await connection.scalar(
            select(text("count(*)"))
            .select_from(transcript_items)
            .where(transcript_items.c.item_id.in_(item_ids))
        )
        return int(result or 0)
