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
from tests.persistence.rag_test_support import embedding_profile, seed_completed_source


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
