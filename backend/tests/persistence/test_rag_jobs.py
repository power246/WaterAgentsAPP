import asyncio
from datetime import UTC, datetime, timedelta

from app.domain import RAGIndexJobStatus
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.rag_repository import PostgresRAGRepository
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import (
    embedding_profile,
    pending_job,
    seed_completed_source,
)


def test_two_repositories_claim_one_job_exactly_once_and_preserve_attempts() -> None:
    async def exercise(settings) -> None:
        first_engine = create_database_engine(settings)
        second_engine = create_database_engine(settings)
        first = PostgresRAGRepository(
            first_engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        second = PostgresRAGRepository(
            second_engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(first_engine)
            generation = await first.create_building_generation(
                embedding_profile(),
                source.completed_at,
                str(source.result_item_id),
            )
            submitted = pending_job(source, generation.generation_id)
            assert await first.enqueue(submitted) == submitted
            assert await second.enqueue(submitted) == submitted

            lease_until = datetime.now(UTC) + timedelta(minutes=1)
            claimed_batches = await asyncio.gather(
                first.claim(generation.generation_id, "worker-a", lease_until, 1),
                second.claim(generation.generation_id, "worker-b", lease_until, 1),
            )
            claimed = tuple(job for batch in claimed_batches for job in batch)
            assert len(claimed) == 1
            assert claimed[0].status is RAGIndexJobStatus.LEASED
            assert claimed[0].attempts == 1

            now = datetime.now(UTC)
            retry = claimed[0].model_copy(
                update={
                    "status": RAGIndexJobStatus.RETRY_WAIT,
                    "next_attempt_at": now - timedelta(seconds=1),
                    "lease_owner": None,
                    "lease_until": None,
                    "last_safe_error_code": "temporary_unavailable",
                    "updated_at": now,
                }
            )
            await first.save(retry)
            reclaimed = await second.claim(
                generation.generation_id,
                "worker-b",
                now + timedelta(minutes=2),
                1,
            )
            assert len(reclaimed) == 1
            assert reclaimed[0].attempts == 2
            assert reclaimed[0].lease_owner == "worker-b"

            completed_at = datetime.now(UTC) + timedelta(seconds=1)
            completed = reclaimed[0].model_copy(
                update={
                    "status": RAGIndexJobStatus.COMPLETED,
                    "lease_owner": None,
                    "lease_until": None,
                    "last_safe_error_code": None,
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                }
            )
            await second.save(completed)
            counts = await first.counts(generation.generation_id)
            assert counts.completed == 1
            assert counts.total == 1
        finally:
            await first_engine.dispose()
            await second_engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
