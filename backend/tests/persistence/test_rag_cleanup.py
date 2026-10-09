import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.application.transcript_memory import (
    GenerationCleanupConflictError,
    GenerationService,
    RAGBackfillCoordinator,
    RAGDiscoveryService,
    RAGMaintenanceConflictError,
    RAGMaintenanceOperations,
)
from app.domain import (
    RAGFailedJobRetryFilter,
    RAGIndexGenerationStatus,
    RAGIndexJobStatus,
)
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.rag_repository import (
    PostgresRAGRepository,
    RAGPersistenceConflictError,
)
from app.infrastructure.persistence.records import RAGConsumptionLinkRecord
from app.infrastructure.persistence.schema import (
    analysis_run_idempotency_tombstones,
    analysis_runs,
    rag_case_projections,
    rag_consumption_links,
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


class _Clock:
    def now(self):
        return datetime.now(UTC) + timedelta(days=30)


def _operations(repository, *, retention_seconds=0):
    discovery = RAGDiscoveryService(repository, batch_size=16)
    backfill = RAGBackfillCoordinator(
        repository,
        discovery,
        max_discovery_batches=16,
    )
    generations = GenerationService(repository)
    return (
        RAGMaintenanceOperations(
            store=repository,
            backfill=backfill,
            generations=generations,
            clock=_Clock(),
            retired_generation_retention_seconds=retention_seconds,
        ),
        generations,
    )


async def _write_case(repository, source, generation_id, profile):
    await repository.enqueue(pending_job(source, generation_id))
    claimed = await repository.claim(
        generation_id,
        "cleanup-worker",
        datetime.now(UTC) + timedelta(minutes=5),
        1,
    )
    case, fragments = ready_projection(source, generation_id, profile)
    await repository.write_ready_projection(claimed[0], case, fragments)
    return case


async def _link(repository, case, consumer):
    link = RAGConsumptionLinkRecord(
        link_id=uuid4(),
        source_generation_id=UUID(case.case_ref.generation_id),
        source_case_id=UUID(case.case_ref.case_id),
        source_run_id=UUID(case.case_ref.run_id),
        source_session_id=UUID(case.case_ref.session_id),
        source_result_item_id=UUID(case.case_ref.result_item_id),
        consumer_run_id=consumer.run_id,
        consumer_session_id=consumer.session_id,
        consumer_item_id=consumer.result_item_id,
        created_at=datetime.now(UTC),
    )
    return await repository.append_consumption_link(link)


async def _count(connection, table):
    return await connection.scalar(select(func.count()).select_from(table))


def test_source_cleanup_without_consumers_is_idempotent_and_removes_derived_rows() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        operations, _ = _operations(repository)
        try:
            source = await seed_completed_source(engine)
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            await _write_case(
                repository,
                source,
                generation.generation_id,
                profile,
            )

            plan = await operations.inspect_cleanup_dependencies(
                (str(source.run_id),)
            )
            assert plan.cascade_required is False
            assert plan.delete_order == (str(source.run_id),)

            deleted = await operations.delete_history_runs(
                (str(source.run_id),)
            )
            assert deleted.deleted_run_ids == (str(source.run_id),)
            repeated = await operations.delete_history_runs(
                (str(source.run_id),)
            )
            assert repeated.deleted_run_ids == ()

            async with engine.connect() as connection:
                assert await _count(connection, analysis_runs) == 0
                assert await _count(connection, transcript_items) == 0
                assert await _count(connection, rag_index_jobs) == 0
                assert await _count(connection, rag_case_projections) == 0
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_source_cleanup_conflicts_then_cascades_transitive_consumers() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        operations, _ = _operations(repository)
        try:
            source = await seed_completed_source(engine, target_comid="10000001")
            consumer = await seed_completed_source(engine, target_comid="10000002")
            transitive = await seed_completed_source(engine, target_comid="10000003")
            source_key, transitive_key = uuid4(), uuid4()
            async with engine.begin() as connection:
                for run_id, key in (
                    (source.run_id, source_key),
                    (transitive.run_id, transitive_key),
                ):
                    await connection.execute(update(analysis_runs).where(
                        analysis_runs.c.run_id == run_id
                    ).values(idempotency_key=key, request_fingerprint=b"f" * 32))
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                transitive.completed_at,
                str(transitive.result_item_id),
            )
            source_case = await _write_case(
                repository,
                source,
                generation.generation_id,
                profile,
            )
            consumer_case = await _write_case(
                repository,
                consumer,
                generation.generation_id,
                profile,
            )
            await _link(repository, source_case, consumer)
            await _link(repository, consumer_case, transitive)

            plan = await operations.inspect_cleanup_dependencies(
                (str(source.run_id),)
            )
            assert plan.cascade_required is True
            assert plan.delete_order == (
                str(transitive.run_id),
                str(consumer.run_id),
                str(source.run_id),
            )
            with pytest.raises(
                RAGPersistenceConflictError,
                match="retained consumer",
            ):
                await operations.delete_history_runs((str(source.run_id),))
            async with engine.connect() as connection:
                assert await _count(connection, analysis_run_idempotency_tombstones) == 0

            deleted = await operations.delete_history_runs(
                (str(source.run_id),),
                cascade_consumers=True,
            )
            assert deleted.delete_order == plan.delete_order
            async with engine.connect() as connection:
                assert await _count(connection, analysis_runs) == 0
                assert await _count(connection, transcript_items) == 0
                assert await _count(connection, rag_consumption_links) == 0
                assert await _count(connection, rag_index_jobs) == 0
                assert await _count(connection, rag_case_projections) == 0
                keys = set((await connection.execute(select(
                    analysis_run_idempotency_tombstones.c.idempotency_key
                ))).scalars())
                assert keys == {source_key, transitive_key}
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_cleanup_rechecks_a_new_consumption_link_after_inspection() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        operations, _ = _operations(repository)
        try:
            source = await seed_completed_source(engine, target_comid="20000001")
            consumer = await seed_completed_source(engine, target_comid="20000002")
            profile = embedding_profile()
            generation = await repository.create_building_generation(
                profile,
                source.completed_at,
                str(source.result_item_id),
            )
            case = await _write_case(
                repository,
                source,
                generation.generation_id,
                profile,
            )
            inspected = await operations.inspect_cleanup_dependencies(
                (str(source.run_id),)
            )
            assert inspected.cascade_required is False

            await _link(repository, case, consumer)
            with pytest.raises(RAGPersistenceConflictError):
                await operations.delete_history_runs((str(source.run_id),))
            async with engine.connect() as connection:
                assert await _count(connection, analysis_runs) == 2
                assert await _count(connection, rag_consumption_links) == 1
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_retired_generation_cleanup_preserves_consumption_source_ownership() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        operations, _ = _operations(repository)
        try:
            source = await seed_completed_source(engine)
            old_profile = embedding_profile(dimensions=3)
            old = await repository.create_building_generation(
                old_profile,
                source.completed_at,
                str(source.result_item_id),
            )
            case = await _write_case(
                repository,
                source,
                old.generation_id,
                old_profile,
            )
            await repository.activate_generation(
                old.generation_id,
                datetime.now(UTC),
            )
            consumer = await seed_completed_source(
                engine,
                target_comid="87654321",
            )
            link = await _link(repository, case, consumer)

            replacement = await repository.create_building_generation(
                embedding_profile(dimensions=4),
                datetime(1970, 1, 1, tzinfo=UTC),
                "00000000-0000-0000-0000-000000000000",
            )
            await repository.activate_generation(
                replacement.generation_id,
                datetime.now(UTC) + timedelta(seconds=1),
            )
            await operations.delete_retired_generation(old.generation_id)

            assert await repository.read_generation(old.generation_id) is None
            assert await repository.read_consumption_links(consumer.run_id) == (link,)
            assert await repository.append_consumption_link(link) == link
            assert await repository.find_source_delete_conflicts((source.run_id,)) == (
                link,
            )
            with pytest.raises(RAGPersistenceConflictError):
                await operations.delete_history_runs((str(source.run_id),))
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_generation_cleanup_switch_retry_and_rebuild_leave_transcript_untouched() -> None:
    async def exercise(settings) -> None:
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        operations, generations = _operations(repository)
        try:
            source = await seed_completed_source(engine)
            old_profile = embedding_profile(dimensions=3)
            old = await repository.create_building_generation(
                old_profile,
                source.completed_at,
                str(source.result_item_id),
            )
            await _write_case(
                repository,
                source,
                old.generation_id,
                old_profile,
            )
            old_active = await repository.activate_generation(
                old.generation_id,
                datetime.now(UTC),
            )
            with pytest.raises(RAGMaintenanceConflictError, match="retired or failed"):
                await operations.delete_retired_generation(old_active.generation_id)

            async with generations.pin_active_generation():
                new_profile = embedding_profile(dimensions=4)
                new = await operations.build_generation(new_profile)
                with pytest.raises(
                    RAGMaintenanceConflictError,
                    match="retired or failed",
                ):
                    await operations.delete_retired_generation(new.generation_id)
                await _write_case(
                    repository,
                    source,
                    new.generation_id,
                    new_profile,
                )
                retired = await operations.retire_generation(
                    old_active.generation_id,
                    replacement_generation_id=new.generation_id,
                )
                assert retired.status is RAGIndexGenerationStatus.RETIRED
                with pytest.raises(GenerationCleanupConflictError, match="reader lease"):
                    await operations.delete_retired_generation(retired.generation_id)

            await operations.delete_retired_generation(old_active.generation_id)
            assert await repository.read_generation(old_active.generation_id) is None
            rebuilt = await operations.build_generation(old_profile)
            assert rebuilt.status is RAGIndexGenerationStatus.BUILDING
            assert rebuilt.generation_id != old_active.generation_id

            await repository.discover_jobs(
                rebuilt.generation_id,
                through=await repository.latest_candidate_cutoff(),
                limit=8,
                discovered_at=datetime.now(UTC),
            )
            leased = (
                await repository.claim(
                    rebuilt.generation_id,
                    "retry-worker",
                    datetime.now(UTC) + timedelta(minutes=1),
                    1,
                )
            )[0]
            failed_at = datetime.now(UTC) + timedelta(seconds=1)
            await repository.save(
                leased.model_copy(
                    update={
                        "status": RAGIndexJobStatus.FAILED,
                        "lease_owner": None,
                        "lease_until": None,
                        "last_safe_error_code": "embedding_timeout",
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                )
            )
            retried = await operations.retry_failed_jobs(
                rebuilt.generation_id,
                RAGFailedJobRetryFilter(
                    safe_error_codes=("embedding_timeout",),
                    limit=1,
                ),
            )
            assert len(retried) == 1
            assert retried[0].status is RAGIndexJobStatus.PENDING
            assert retried[0].attempts == 0

            failed = await repository.fail_generation(
                rebuilt.generation_id,
                datetime.now(UTC) + timedelta(days=31),
                "operator_abandoned_rebuild",
            )
            await operations.delete_retired_generation(failed.generation_id)
            assert await repository.read_generation(failed.generation_id) is None

            async with engine.connect() as connection:
                assert await _count(connection, analysis_runs) == 1
                assert await _count(connection, transcript_items) == 10
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
