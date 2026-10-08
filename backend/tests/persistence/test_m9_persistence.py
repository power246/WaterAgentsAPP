"""M9 durable boundaries against an isolated PostgreSQL test schema."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.application.settings_admin.errors import (
    AdminOperationConflictError, SettingsRevisionConflictError,
)
from app.domain.settings_admin import (
    AdminOperation, AdminOperationState, AdminProgress, AdminSafeError,
    SettingsSnapshot, UnitPolicyVersion,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.admin_operation_repository import (
    PostgresAdminOperationRepository,
)
from app.infrastructure.persistence.cli import run as run_database_cli
from app.infrastructure.persistence.database import (
    IncompatibleSchemaError, alembic_config, check_database_schema,
    create_database_engine,
)
from app.infrastructure.persistence.rag_repository import PostgresRAGRepository
from app.infrastructure.persistence.schema import (
    admin_operations, analysis_run_idempotency_tombstones, analysis_runs,
    rag_index_generations, runtime_settings, unit_policy_entries, unit_policy_versions,
    transcript_items,
)
from app.infrastructure.persistence.settings_repository import PostgresSettingsRepository
from app.infrastructure.persistence.tombstone_repository import PostgresTombstoneRepository
from app.ports.errors import PortUnavailableError
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import embedding_profile, seed_completed_source


def _snapshot(policy, revision: int, *, timezone: str = "UTC") -> SettingsSnapshot:
    return SettingsSnapshot(
        revision=revision,
        updated_at=datetime.now(UTC),
        defaults={
            "analysis_timezone": timezone,
            "parser_model": {"provider": "openai", "model": "parser"},
            "waterbody_model": {"provider": "openai", "model": "waterbody"},
            "memory_mode": "stateless", "time_resolution": "12h",
            "desired_embedding_profile_id": "sha256:" + "a" * 64,
        },
        unit_policy=policy.model_dump(exclude={"created_at"}),
    )


def _pending(*, resource: str = "run-set") -> AdminOperation:
    return AdminOperation(
        operation_id=uuid4(), kind="history_cleanup", state="pending",
        resource_identity=resource, progress=AdminProgress(),
        created_at=datetime.now(UTC),
    )


def test_m9_migration_round_trip_preserves_m8_transcripts_and_registry() -> None:
    async def exercise(settings):
        engine = create_database_engine(settings)
        try:
            registry = load_parameter_capability_registry()
            async with engine.connect() as connection:
                digest = await connection.scalar(select(unit_policy_versions.c.registry_digest))
                entries = tuple((await connection.execute(select(
                    unit_policy_entries.c.parameter_code_system,
                    unit_policy_entries.c.parameter_code,
                    unit_policy_entries.c.target_unit_id,
                ).order_by(unit_policy_entries.c.parameter_code_system,
                           unit_policy_entries.c.parameter_code))).tuples())
            assert digest == registry.manifest_digest
            assert entries == tuple(sorted(
                (item.key.code_system, item.key.code, item.unit_policy_id)
                for item in registry.capabilities
            ))
            source = await seed_completed_source(engine)
            return source
        finally:
            await engine.dispose()

    async def verify_survival(settings, source):
        engine = create_database_engine(settings)
        try:
            repository = PostgresRAGRepository(
                engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
            )
            generation = await repository.create_building_generation(
                embedding_profile(), source.completed_at, str(source.result_item_id),
            )
            async with engine.begin() as connection:
                await connection.execute(update(rag_index_generations).where(
                    rag_index_generations.c.generation_id == UUID(generation.generation_id)
                ).values(status="active", activated_at=datetime.now(UTC)))
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(
                    analysis_runs
                ).where(analysis_runs.c.run_id == source.run_id)) == 1
            return generation.generation_id
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        source = asyncio.run(exercise(settings))
        registry = load_parameter_capability_registry()
        with patch(
            "app.infrastructure.persistence.database.load_parameter_capability_registry",
            return_value=SimpleNamespace(
                manifest_digest="f" * 64, capabilities=registry.capabilities,
            ),
        ):
            with pytest.raises(IncompatibleSchemaError):
                asyncio.run(check_database_schema(settings))
        command.downgrade(alembic_config(settings), "20261007_01")
        generation_id = asyncio.run(verify_survival(settings, source))
        assert run_database_cli(["upgrade", "--test-database"]) == 0
        assert asyncio.run(check_database_schema(settings)) == "20261008_01"
        assert run_database_cli(["check", "--test-database"]) == 0
        async def check_preserved():
            engine = create_database_engine(settings)
            try:
                async with engine.connect() as connection:
                    assert await connection.scalar(select(rag_index_generations.c.status).where(
                        rag_index_generations.c.generation_id == UUID(generation_id)
                    )) == "active"
                    assert await connection.scalar(select(func.count()).select_from(
                        analysis_runs
                    ).where(analysis_runs.c.run_id == source.run_id)) == 1
            finally:
                await engine.dispose()
        asyncio.run(check_preserved())


def test_settings_singleton_constraints_append_only_and_cas_race() -> None:
    async def exercise(settings):
        engine = create_database_engine(settings)
        repository = PostgresSettingsRepository(
            engine, load_parameter_capability_registry(),
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            assert await repository.read_current() is None
            policy = await repository.read_unit_policy(1)
            assert policy is not None and len(policy.entries) == 154
            first = await repository.compare_and_swap(
                expected_revision=0, replacement=_snapshot(policy, 1),
            )
            assert await repository.read_current() == first
            alternatives = (
                _snapshot(policy, 2, timezone="Europe/London"),
                _snapshot(policy, 2, timezone="Asia/Shanghai"),
            )
            outcome = await asyncio.gather(*(
                repository.compare_and_swap(expected_revision=1, replacement=item)
                for item in alternatives
            ), return_exceptions=True)
            assert sum(isinstance(item, SettingsSnapshot) for item in outcome) == 1
            assert sum(isinstance(item, SettingsRevisionConflictError) for item in outcome) == 1
            assert (await repository.read_current()) in alternatives
            policy2 = UnitPolicyVersion(
                version=2, registry_digest=policy.registry_digest,
                entries=policy.entries, created_at=datetime.now(UTC),
            )
            third = _snapshot(policy2, 3)
            assert await repository.append_unit_policy(
                expected_revision=2, expected_unit_version=1,
                policy=policy2, replacement=third,
            ) == third
            assert await repository.read_unit_policy(2) == policy2
            assert await repository.read_unit_policy(1) == policy
            async with engine.begin() as connection:
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(insert(runtime_settings).values(
                            singleton_id=2, schema_version=1, revision=3,
                            analysis_timezone="UTC", parser_provider="openai",
                            parser_model="parser", waterbody_provider="openai",
                            waterbody_model="waterbody", memory_mode="stateless",
                            time_resolution="12h",
                            desired_embedding_profile_id="sha256:" + "a" * 64,
                            active_unit_policy_version=1, updated_at=datetime.now(UTC),
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(unit_policy_versions).where(
                            unit_policy_versions.c.version == 1
                        ).values(registry_digest="b" * 64))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(insert(unit_policy_entries).values(
                            version=2, parameter_code_system="usgs",
                            parameter_code="99999", target_unit_id="mg/L",
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(runtime_settings).where(
                            runtime_settings.c.singleton_id == 1
                        ).values(active_unit_policy_version=999))
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_admin_operation_exact_once_state_and_json_guards() -> None:
    async def exercise(settings):
        engine = create_database_engine(settings)
        repository = PostgresAdminOperationRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        one, two = _pending(), _pending()
        key1, key2 = uuid4(), uuid4()
        fp1, fp2 = b"a" * 32, b"b" * 32
        token1, token2 = b"c" * 32, b"d" * 32
        try:
            result = await asyncio.gather(
                repository.create(one, idempotency_key=key1,
                                  request_fingerprint=fp1,
                                  confirmation_token_digest=token1),
                repository.create(two, idempotency_key=key2,
                                  request_fingerprint=fp2,
                                  confirmation_token_digest=token2),
                return_exceptions=True,
            )
            assert sum(isinstance(item, AdminOperation) for item in result) == 1, str(result[0].__cause__)
            assert sum(isinstance(item, AdminOperationConflictError) for item in result) == 1
            winner = result[0] if isinstance(result[0], AdminOperation) else result[1]
            key, fingerprint, token = (
                (key1, fp1, token1) if winner.operation_id == one.operation_id
                else (key2, fp2, token2)
            )
            assert await repository.create(
                winner, idempotency_key=key,
                request_fingerprint=fingerprint,
                confirmation_token_digest=token,
            ) == winner
            with pytest.raises(AdminOperationConflictError):
                await repository.create(
                    _pending(), idempotency_key=key,
                    request_fingerprint=b"z" * 32,
                    confirmation_token_digest=b"x" * 32,
                )
            with pytest.raises(AdminOperationConflictError):
                await repository.create(
                    _pending(), idempotency_key=uuid4(),
                    request_fingerprint=fp1,
                    confirmation_token_digest=token,
                )
            page, cursor = await repository.list_recent(limit=1, cursor=None)
            assert page == (winner,) and cursor is None
            with pytest.raises(ValueError, match="cursor"):
                await repository.list_recent(limit=1, cursor="!" * 300)
            assert await repository.get(winner.operation_id) == winner
            running = winner.model_copy(update={
                "state": AdminOperationState.RUNNING,
                "started_at": winner.created_at + timedelta(seconds=1),
            })
            assert await repository.compare_and_swap_state(
                running, expected=AdminOperationState.PENDING
            ) == running
            with pytest.raises(AdminOperationConflictError):
                await repository.compare_and_swap_state(
                    running, expected=AdminOperationState.PENDING,
                )
            running = running.model_copy(update={
                "progress": AdminProgress(processed_items=1, total_items=2),
            })
            assert await repository.compare_and_swap_state(
                running, expected=AdminOperationState.RUNNING,
            ) == running
            async with engine.begin() as connection:
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(admin_operations).where(
                            admin_operations.c.operation_id == winner.operation_id
                        ).values(
                            state="failed",
                            finished_at=running.started_at + timedelta(seconds=1),
                            safe_error={"code": "x", "message": "x",
                                        "retryable": False, "secret": "forbidden"},
                        ))
            finished = running.model_copy(update={
                "state": AdminOperationState.FAILED,
                "finished_at": running.started_at + timedelta(seconds=1),
                "safe_error": AdminSafeError(code="test_failure", message="Failed."),
            })
            await repository.compare_and_swap_state(
                finished, expected=AdminOperationState.RUNNING,
            )
            assert await repository.get(winner.operation_id) == finished
            with pytest.raises(AdminOperationConflictError):
                await repository.compare_and_swap_state(
                    finished, expected=AdminOperationState.FAILED,
                )
            another = _pending(resource="other-run")
            await repository.create(
                another, idempotency_key=uuid4(), request_fingerprint=b"e" * 32,
                confirmation_token_digest=b"g" * 32,
            )
            async with engine.begin() as connection:
                for summary in (
                    {"action": "history_cleanup"},
                    {"action": "history_cleanup", "resource_identity": "other-run",
                     "secret": "forbidden"},
                ):
                    with pytest.raises(IntegrityError):
                        async with connection.begin_nested():
                            await connection.execute(update(admin_operations).where(
                                admin_operations.c.operation_id == another.operation_id
                            ).values(safe_request_summary=summary))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(admin_operations).where(
                            admin_operations.c.operation_id == another.operation_id
                        ).values(
                            state="succeeded", started_at=another.created_at,
                            finished_at=another.created_at + timedelta(seconds=1),
                            safe_result={"kind": "history_cleanup", "deleted_run_ids": []},
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(admin_operations).where(
                            admin_operations.c.operation_id == another.operation_id
                        ).values(
                            state="succeeded", started_at=another.created_at,
                            finished_at=another.created_at + timedelta(seconds=1),
                            safe_result={
                                "kind": "history_cleanup",
                                "deleted_run_ids": [{"secret": "forbidden"}],
                                "retained_idempotency_tombstones": 0,
                            },
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(admin_operations).where(
                            admin_operations.c.operation_id == another.operation_id
                        ).values(
                            state="succeeded", started_at=another.created_at,
                            finished_at=another.created_at + timedelta(seconds=1),
                            safe_result={
                                "kind": "history_cleanup",
                                "deleted_run_ids": [str(uuid4()) for _ in range(150)],
                                "retained_idempotency_tombstones": 0,
                            },
                        ))
            assert await repository.reconcile_interrupted(
                now=datetime.now(UTC) + timedelta(seconds=5)
            ) == 1
            restarted = PostgresAdminOperationRepository(
                engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
            )
            assert (await restarted.get(another.operation_id)).state is AdminOperationState.INTERRUPTED
            page, cursor = await restarted.list_recent(limit=1, cursor=None)
            assert len(page) == 1 and cursor is not None
            second_page, _ = await restarted.list_recent(limit=1, cursor=cursor)
            assert second_page == (finished,)
            async with engine.begin() as connection:
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(update(admin_operations).where(
                            admin_operations.c.operation_id == winner.operation_id
                        ).values(resource_identity="changed"))
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_cleanup_tombstone_is_atomic_and_blocks_key_reuse() -> None:
    async def exercise(settings):
        engine = create_database_engine(settings)
        repository = PostgresRAGRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        tombstones = PostgresTombstoneRepository(
            engine, operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            source = await seed_completed_source(engine)
            key, fingerprint = uuid4(), b"f" * 32
            async with engine.begin() as connection:
                await connection.execute(update(analysis_runs).where(
                    analysis_runs.c.run_id == source.run_id
                ).values(idempotency_key=key, request_fingerprint=fingerprint))
                await connection.execute(text("""
                    CREATE FUNCTION m9_test_reject_session_delete() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$
                """))
                await connection.execute(text("""
                    CREATE TRIGGER tr_m9_test_reject_session_delete
                    BEFORE DELETE ON analysis_sessions
                    FOR EACH ROW EXECUTE FUNCTION m9_test_reject_session_delete()
                """))
            with pytest.raises(PortUnavailableError):
                await repository.delete_history_runs((str(source.run_id),),
                                                     cascade_consumers=False)
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(
                    transcript_items
                ).where(transcript_items.c.run_id == source.run_id)) > 0
            async with engine.begin() as connection:
                await connection.execute(text("DROP TRIGGER tr_m9_test_reject_session_delete ON analysis_sessions"))
                await connection.execute(text("DROP FUNCTION m9_test_reject_session_delete()"))
            async with engine.begin() as connection:
                await connection.execute(text("""
                    CREATE FUNCTION m9_test_reject_tombstone() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$
                """))
                await connection.execute(text("""
                    CREATE TRIGGER tr_m9_test_reject_tombstone
                    BEFORE INSERT ON analysis_run_idempotency_tombstones
                    FOR EACH ROW EXECUTE FUNCTION m9_test_reject_tombstone()
                """))
            with pytest.raises(PortUnavailableError):
                await repository.delete_history_runs((str(source.run_id),),
                                                     cascade_consumers=False)
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(
                    analysis_runs
                ).where(analysis_runs.c.run_id == source.run_id)) == 1
                assert await connection.scalar(select(func.count()).select_from(
                    analysis_run_idempotency_tombstones
                )) == 0
            async with engine.begin() as connection:
                await connection.execute(text("DROP TRIGGER tr_m9_test_reject_tombstone ON analysis_run_idempotency_tombstones"))
                await connection.execute(text("DROP FUNCTION m9_test_reject_tombstone()"))
            result = await repository.delete_history_runs((str(source.run_id),),
                                                         cascade_consumers=False)
            assert result.deleted_run_ids == (str(source.run_id),)
            tombstone = await tombstones.get(key)
            assert tombstone is not None
            assert tombstone.request_fingerprint == fingerprint
            assert tombstone.deleted_run_id == source.run_id
            assert (await repository.delete_history_runs((str(source.run_id),),
                                                        cascade_consumers=False)).deleted_run_ids == ()
            other = await seed_completed_source(engine)
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(update(analysis_runs).where(
                        analysis_runs.c.run_id == other.run_id
                    ).values(idempotency_key=key, request_fingerprint=fingerprint))
        finally:
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
