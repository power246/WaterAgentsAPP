import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text

from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceGroup,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
    RAGConsumptionLink,
)
from app.application.transcripts.models import (
    StoredTranscriptItem,
    TranscriptStorageBatch,
)
from app.infrastructure.persistence.database import (
    alembic_config,
    create_database_engine,
    upgrade_database,
)
from app.infrastructure.persistence.rag_repository import PostgresRAGRepository
from app.infrastructure.persistence.schema import (
    rag_consumption_links,
    transcript_items,
)
from app.infrastructure.persistence.store import PostgresTranscriptStore
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import (
    embedding_profile,
    pending_job,
    ready_projection,
    seed_completed_source,
)


async def _ready_case(repository, source):
    profile = embedding_profile()
    generation = await repository.create_building_generation(
        profile,
        source.completed_at,
        str(source.result_item_id),
    )
    await repository.enqueue(pending_job(source, generation.generation_id))
    claimed = await repository.claim(
        generation.generation_id,
        "consumption-link-worker",
        datetime.now(UTC) + timedelta(minutes=10),
        1,
    )
    case, fragments = ready_projection(
        source,
        generation.generation_id,
        profile,
    )
    await repository.write_ready_projection(claimed[0], case, fragments)
    return case


def _consumer_batch(consumer, case, *, valid_source=True):
    item_id = uuid4()
    created_at = consumer.completed_at + timedelta(minutes=1)
    item = StoredTranscriptItem(
        item_id=item_id,
        run_id=consumer.run_id,
        session_id=consumer.session_id,
        transcript_sequence=11 if valid_source else 12,
        created_at=created_at,
        content_type="evidence_source_records",
        content_schema_version=1,
        tags=(),
        rag_eligible=False,
        inline_body="{}",
        content_bytes=2,
    )
    link = RAGConsumptionLink(
        link_id=uuid4(),
        source_generation_id=UUID(case.case_ref.generation_id),
        source_case_id=(
            UUID(case.case_ref.case_id) if valid_source else uuid4()
        ),
        source_run_id=UUID(case.case_ref.run_id),
        source_session_id=UUID(case.case_ref.session_id),
        source_result_item_id=UUID(case.case_ref.result_item_id),
        consumer_run_id=consumer.run_id,
        consumer_session_id=consumer.session_id,
        consumer_item_id=item_id,
        created_at=created_at,
    )
    return PersistenceWriteBatch(
        run_id=consumer.run_id,
        groups=(PersistenceGroup(item_ids=(item_id,)),),
        transcript=TranscriptStorageBatch(items=(item,)),
        rag_consumption_links=(link,),
    )


def test_source_record_and_consumption_link_commit_or_rollback_together() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            consumer = await seed_completed_source(
                engine,
                target_comid="87654321",
            )
            case = await _ready_case(repository, source)
            committed = _consumer_batch(consumer, case, valid_source=True)

            await store.write_batch(committed)

            assert await store.verify_batch(committed) is BatchVerification.COMMITTED
            async with engine.connect() as connection:
                assert await connection.scalar(
                    select(func.count())
                    .select_from(rag_consumption_links)
                    .where(
                        rag_consumption_links.c.consumer_item_id
                        == committed.item_ids[0]
                    )
                ) == 1

            rejected = _consumer_batch(consumer, case, valid_source=False)
            with pytest.raises(PersistenceWriteConflictError):
                await store.write_batch(rejected)

            async with engine.connect() as connection:
                item_count = await connection.scalar(
                    select(func.count())
                    .select_from(transcript_items)
                    .where(transcript_items.c.item_id == rejected.item_ids[0])
                )
                link_count = await connection.scalar(
                    select(func.count())
                    .select_from(rag_consumption_links)
                    .where(
                        rag_consumption_links.c.consumer_item_id
                        == rejected.item_ids[0]
                    )
                )
            assert (item_count, link_count) == (0, 0)
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_consumer_batch_commits_after_its_source_generation_is_deleted() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            case = await _ready_case(repository, source)
            retired_generation_id = case.case_ref.generation_id
            await repository.activate_generation(
                retired_generation_id,
                datetime.now(UTC),
            )
            consumer = await seed_completed_source(
                engine,
                target_comid="87654321",
            )
            batch = _consumer_batch(consumer, case)
            replacement = await repository.create_building_generation(
                embedding_profile(dimensions=4),
                datetime(1970, 1, 1, tzinfo=UTC),
                "00000000-0000-0000-0000-000000000000",
            )
            await repository.activate_generation(
                replacement.generation_id,
                datetime.now(UTC) + timedelta(seconds=1),
            )
            await repository.delete_generation(retired_generation_id)

            await store.write_batch(batch)

            assert await repository.read_generation(retired_generation_id) is None
            assert await store.verify_batch(batch) is BatchVerification.COMMITTED
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_legacy_consumption_schema_upgrades_without_losing_a_source_link() -> None:
    async def seed(settings):
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        store = PostgresTranscriptStore(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            consumer = await seed_completed_source(engine, target_comid="87654321")
            case = await _ready_case(repository, source)
            batch = _consumer_batch(consumer, case)
            await store.write_batch(batch)
            return source, consumer, batch.rag_consumption_links[0]
        finally:
            await engine.dispose()

    async def restore_legacy_shape(settings):
        engine = create_database_engine(settings)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(
                    "ALTER TABLE rag_consumption_links "
                    "DROP CONSTRAINT fk_rag_consumption_source_item"
                ))
                await connection.execute(text("DROP INDEX ix_rag_consumption_source"))
                await connection.execute(text(
                    "ALTER TABLE rag_consumption_links "
                    "DROP COLUMN source_run_id, DROP COLUMN source_session_id"
                ))
                await connection.execute(text("""
                    ALTER TABLE rag_consumption_links
                    ADD CONSTRAINT fk_rag_consumption_source_case
                    FOREIGN KEY (source_generation_id, source_case_id, source_result_item_id)
                    REFERENCES rag_case_projections
                        (generation_id, case_id, source_result_item_id)
                    ON DELETE RESTRICT
                """))
                await connection.execute(text(
                    "CREATE INDEX ix_rag_consumption_source ON rag_consumption_links "
                    "(source_generation_id, source_case_id)"
                ))
                await connection.execute(text(
                    "ALTER TABLE rag_fragment_projections "
                    "DROP CONSTRAINT pk_rag_fragment_projections"
                ))
                await connection.execute(text(
                    "ALTER TABLE rag_fragment_projections "
                    "ADD CONSTRAINT pk_rag_fragment_projections "
                    "PRIMARY KEY (generation_id, case_id, source_fragment_item_id)"
                ))
        finally:
            await engine.dispose()

    async def verify(settings, source, consumer, link):
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            async with engine.connect() as connection:
                saved = (await connection.execute(
                    select(rag_consumption_links).where(
                        rag_consumption_links.c.link_id == link.link_id
                    )
                )).mappings().one()
                assert saved["source_run_id"] == source.run_id
                assert saved["source_session_id"] == source.session_id
                assert saved["source_result_item_id"] == source.result_item_id
                assert await connection.scalar(text("""
                    SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conrelid = to_regclass('rag_fragment_projections')
                      AND conname = 'pk_rag_fragment_projections'
                """)) == (
                    "PRIMARY KEY (generation_id, case_id, source_fragment_item_id, "
                    "parameter_key, window_id)"
                )
            plan = await repository.inspect_cleanup_dependencies((str(source.run_id),))
            assert plan.cascade_required is True
            assert plan.delete_order == (str(consumer.run_id), str(source.run_id))
            assert tuple(item.link_id for item in plan.dependencies) == (str(link.link_id),)
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        source, consumer, link = asyncio.run(seed(settings))
        command.downgrade(alembic_config(settings), "20261008_03")
        asyncio.run(restore_legacy_shape(settings))
        assert upgrade_database(settings) == "20261020_02"
        asyncio.run(verify(settings, source, consumer, link))
