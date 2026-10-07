import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.rag_repository import (
    PostgresRAGRepository,
    RAGPersistenceConflictError,
)
from app.domain import RAGIndexGenerationStatus
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import (
    embedding_profile,
    pending_job,
    ready_projection,
    seed_completed_source,
)


def test_generation_switch_is_atomic_and_normal_read_returns_only_active() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            first = await repository.create_building_generation(
                embedding_profile(),
                source.completed_at,
                str(source.result_item_id),
            )
            assert await repository.read_active_generation() is None
            with pytest.raises(RAGPersistenceConflictError):
                await repository.create_building_generation(
                    embedding_profile(dimensions=4),
                    source.completed_at,
                    str(source.result_item_id),
                )

            activated_at = datetime.now(UTC)
            active_first = await repository.activate_generation(
                first.generation_id,
                activated_at,
            )
            assert active_first.status is RAGIndexGenerationStatus.ACTIVE

            second = await repository.create_building_generation(
                embedding_profile(dimensions=4),
                source.completed_at,
                str(source.result_item_id),
            )
            active_second = await repository.activate_generation(
                second.generation_id,
                activated_at + timedelta(seconds=1),
            )
            generations = await repository.list_generations()

            assert active_second.status is RAGIndexGenerationStatus.ACTIVE
            assert (await repository.read_active_generation()) == active_second
            assert [item.status for item in generations] == [
                RAGIndexGenerationStatus.RETIRED,
                RAGIndexGenerationStatus.ACTIVE,
            ]
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_failed_generation_is_not_returned_as_active_and_can_be_deleted() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            building = await repository.create_building_generation(
                embedding_profile(),
                source.completed_at,
                str(source.result_item_id),
            )
            failed = await repository.fail_generation(
                building.generation_id,
                datetime.now(UTC),
                "embedding_protocol_invalid",
            )
            assert failed.status is RAGIndexGenerationStatus.FAILED
            assert await repository.read_active_generation() is None

            await repository.delete_generation(failed.generation_id)
            assert await repository.read_generation(failed.generation_id) is None
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_activation_requires_terminal_counts_and_validated_projection_closure() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            profile = embedding_profile()
            building = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            job = await repository.enqueue(
                pending_job(source, building.generation_id)
            )
            with pytest.raises(RAGPersistenceConflictError, match="validated"):
                await repository.activate_generation(
                    building.generation_id,
                    datetime.now(UTC),
                )

            leased = (
                await repository.claim(
                    building.generation_id,
                    "m4-generation-validator",
                    datetime.now(UTC) + timedelta(minutes=1),
                    1,
                )
            )[0]
            assert leased.job_id == job.job_id
            case, fragments = ready_projection(
                source,
                building.generation_id,
                profile,
            )
            await repository.write_ready_projection(leased, case, fragments)

            active = await repository.activate_generation(
                building.generation_id,
                datetime.now(UTC),
            )
            assert active.status is RAGIndexGenerationStatus.ACTIVE
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_activate_and_fail_compete_for_one_building_terminal_transition() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        first_repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        second_repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            building = await first_repository.create_building_generation(
                embedding_profile(),
                source.completed_at,
                str(source.result_item_id),
            )
            transitions = await asyncio.gather(
                first_repository.activate_generation(
                    building.generation_id,
                    datetime.now(UTC),
                ),
                second_repository.fail_generation(
                    building.generation_id,
                    datetime.now(UTC),
                    "embedding_protocol_invalid",
                ),
                return_exceptions=True,
            )
            successes = tuple(
                item for item in transitions if not isinstance(item, Exception)
            )
            failures = tuple(
                item for item in transitions if isinstance(item, Exception)
            )
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], RAGPersistenceConflictError)
            persisted = await first_repository.read_generation(building.generation_id)
            assert persisted is not None
            assert persisted.status in {
                RAGIndexGenerationStatus.ACTIVE,
                RAGIndexGenerationStatus.FAILED,
            }
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
