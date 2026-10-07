import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.domain import EmbeddingVector, RAGIndexJobStatus
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.rag_repository import (
    PostgresRAGRepository,
    RAGPersistenceConflictError,
)
from app.infrastructure.persistence.records import RAGConsumptionLinkRecord
from app.infrastructure.persistence.schema import (
    rag_case_projections,
    rag_case_windows,
    rag_consumption_links,
    rag_fragment_projections,
    rag_index_jobs,
    transcript_items,
)
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import (
    embedding_profile,
    pending_job,
    ready_projection,
    seed_completed_source,
)


async def _leased_job(repository, source, generation_id):
    await repository.enqueue(pending_job(source, generation_id))
    claimed = await repository.claim(
        generation_id,
        "projection-worker",
        datetime.now(UTC) + timedelta(minutes=1),
        1,
    )
    assert len(claimed) == 1
    return claimed[0]


def test_case_windows_fragments_and_job_completion_are_atomic_and_idempotent() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            job = await _leased_job(repository, source, generation.generation_id)
            case, fragments = ready_projection(
                source,
                generation.generation_id,
                profile,
            )
            await repository.write_ready_projection(job, case, fragments)
            await repository.write_ready_projection(job, case, fragments)
            conflicting = case.model_copy(
                update={"projection_digest": "sha256:different-projection"}
            )
            with pytest.raises(RAGPersistenceConflictError):
                await repository.write_ready_projection(job, conflicting, fragments)

            async with engine.connect() as connection:
                count_values = []
                for table in (
                    rag_case_projections,
                    rag_case_windows,
                    rag_fragment_projections,
                ):
                    count_values.append(
                        int(
                            await connection.scalar(
                                select(func.count()).select_from(table)
                            )
                            or 0
                        )
                    )
                counts = tuple(count_values)
                status = await connection.scalar(
                    select(rag_index_jobs.c.status).where(
                        rag_index_jobs.c.job_id == UUID(job.job_id)
                    )
                )
                window = (
                    await connection.execute(
                        select(
                            rag_case_windows.c.local_months,
                            rag_case_windows.c.season_bucket,
                            rag_case_windows.c.season_year,
                            rag_case_windows.c.duration_seconds,
                            rag_case_windows.c.resolution,
                            rag_case_windows.c.parameter_keys,
                        )
                    )
                ).one()
            assert counts == (1, 1, 1)
            assert status == RAGIndexJobStatus.COMPLETED.value
            assert window == (
                [7],
                "JJA",
                2026,
                604_800,
                "24h",
                ['{"code":"00300","code_system":"usgs"}'],
            )
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_projection_failure_rolls_back_case_windows_fragments_and_job_state() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            job = await _leased_job(repository, source, generation.generation_id)
            case, fragments = ready_projection(
                source,
                generation.generation_id,
                profile,
            )
            invalid_fragment = fragments[0].model_copy(
                update={"source_fragment_item_id": str(uuid4())}
            )

            with pytest.raises(RAGPersistenceConflictError):
                await repository.write_ready_projection(
                    job,
                    case,
                    (invalid_fragment,),
                )

            async with engine.connect() as connection:
                case_count = await connection.scalar(
                    select(func.count()).select_from(rag_case_projections)
                )
                window_count = await connection.scalar(
                    select(func.count()).select_from(rag_case_windows)
                )
                fragment_count = await connection.scalar(
                    select(func.count()).select_from(rag_fragment_projections)
                )
                status = await connection.scalar(
                    select(rag_index_jobs.c.status).where(
                        rag_index_jobs.c.job_id == UUID(job.job_id)
                    )
                )
            assert (case_count, window_count, fragment_count) == (0, 0, 0)
            assert status == RAGIndexJobStatus.LEASED.value
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_database_rejects_projection_dimension_mismatch() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            profile = embedding_profile(dimensions=3)
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            job = await _leased_job(repository, source, generation.generation_id)
            case, fragments = ready_projection(
                source,
                generation.generation_id,
                profile,
            )
            wrong_dimension = case.model_copy(
                update={"embedding": EmbeddingVector(values=(0.1, 0.2))}
            )
            with pytest.raises(RAGPersistenceConflictError):
                await repository.write_ready_projection(
                    job,
                    wrong_dimension,
                    fragments,
                )
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_consumption_links_are_idempotent_restrict_source_and_cascade_consumer() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            consumer = await seed_completed_source(engine, target_comid="87654321")
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            job = await _leased_job(repository, source, generation.generation_id)
            case, fragments = ready_projection(
                source,
                generation.generation_id,
                profile,
            )
            await repository.write_ready_projection(job, case, fragments)
            link = RAGConsumptionLinkRecord(
                link_id=uuid4(),
                source_generation_id=UUID(generation.generation_id),
                source_case_id=UUID(case.case_ref.case_id),
                source_result_item_id=source.result_item_id,
                consumer_run_id=consumer.run_id,
                consumer_session_id=consumer.session_id,
                consumer_item_id=consumer.fragment_item_id,
                created_at=datetime.now(UTC),
            )
            assert await repository.append_consumption_link(link) == link
            assert await repository.append_consumption_link(link) == link
            assert await repository.read_consumption_links(consumer.run_id) == (link,)
            assert await repository.find_source_delete_conflicts((source.run_id,)) == (
                link,
            )

            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(transcript_items).where(
                            transcript_items.c.item_id == source.result_item_id
                        )
                    )

            async with engine.begin() as connection:
                await connection.execute(
                    delete(transcript_items).where(
                        transcript_items.c.item_id == consumer.fragment_item_id
                    )
                )
            async with engine.connect() as connection:
                remaining = await connection.scalar(
                    select(func.count()).select_from(rag_consumption_links)
                )
            assert remaining == 0
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
