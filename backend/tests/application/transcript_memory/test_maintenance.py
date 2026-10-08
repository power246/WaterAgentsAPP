import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.application.transcript_memory import (
    GenerationCleanupConflictError,
    GenerationService,
    RAGMaintenanceConflictError,
    RAGMaintenanceOperations,
)
from app.domain import (
    EmbeddingProfile,
    RAGCleanupPlan,
    RAGCleanupResult,
    RAGFailedJobRetryFilter,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGJobCounts,
    RAGProfileHandoffPhase,
)

NOW = datetime(2026, 10, 8, 8, tzinfo=UTC)
OLD_ID = "91000000-0000-4000-8000-000000000001"
NEW_ID = "91000000-0000-4000-8000-000000000002"


def _profile(model: str) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="test-provider",
        model=model,
        output_dimensions=3,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )


def _generation(
    identifier: str,
    status: RAGIndexGenerationStatus,
    profile: EmbeddingProfile,
) -> RAGIndexGeneration:
    values = {
        "generation_id": identifier,
        "profile": profile,
        "status": status,
        "source_cutoff_created_at": NOW,
        "source_cutoff_item_id": "91000000-0000-4000-8000-000000000099",
        "created_at": NOW,
    }
    if status is RAGIndexGenerationStatus.ACTIVE:
        values["activated_at"] = NOW + timedelta(seconds=1)
    return RAGIndexGeneration.model_validate(values)


class _Clock:
    def now(self):
        return NOW + timedelta(hours=1)


class _Backfill:
    def __init__(self, store):
        self.store = store

    async def ensure_building_generation(self, profile):
        return next(
            (
                generation
                for generation in self.store.generations.values()
                if generation.status is RAGIndexGenerationStatus.BUILDING
                and generation.profile == profile
            ),
            None,
        )


class _Store:
    def __init__(self, old, building):
        self.generations = {
            old.generation_id: old,
            building.generation_id: building,
        }
        self.deleted = []
        self.retried = []

    async def list_generations(self):
        return tuple(self.generations.values())

    async def read_generation(self, generation_id):
        return self.generations.get(generation_id)

    async def read_active_generation(self):
        return next(
            (
                item
                for item in self.generations.values()
                if item.status is RAGIndexGenerationStatus.ACTIVE
            ),
            None,
        )

    async def counts(self, generation_id):
        assert generation_id in self.generations
        return RAGJobCounts(pending=1)

    async def activate_generation(self, generation_id, activated_at):
        current = await self.read_active_generation()
        if current is not None:
            self.generations[current.generation_id] = current.model_copy(
                update={
                    "status": RAGIndexGenerationStatus.RETIRED,
                    "retired_at": activated_at,
                }
            )
        building = self.generations[generation_id]
        active = building.model_copy(
            update={
                "status": RAGIndexGenerationStatus.ACTIVE,
                "activated_at": activated_at,
            }
        )
        self.generations[generation_id] = active
        return active

    async def retry_failed_jobs(
        self,
        generation_id,
        safe_error_codes,
        limit,
        retried_at,
    ):
        self.retried.append(
            (generation_id, safe_error_codes, limit, retried_at)
        )
        return ()

    async def delete_generation(self, generation_id):
        self.deleted.append(generation_id)
        self.generations.pop(generation_id, None)

    async def inspect_cleanup_dependencies(self, run_ids):
        return RAGCleanupPlan(
            requested_run_ids=run_ids,
            delete_run_ids=run_ids,
            delete_order=run_ids,
            cascade_required=False,
        )

    async def delete_history_runs(self, run_ids, *, cascade_consumers):
        assert cascade_consumers is False
        return RAGCleanupResult(
            requested_run_ids=run_ids,
            deleted_run_ids=run_ids,
            delete_order=run_ids,
        )


def test_internal_operations_enforce_switch_reader_drain_and_credential_order() -> None:
    async def exercise() -> None:
        old_profile = _profile("embedding-old")
        desired = _profile("embedding-new")
        old = _generation(
            OLD_ID,
            RAGIndexGenerationStatus.ACTIVE,
            old_profile,
        )
        building = _generation(
            NEW_ID,
            RAGIndexGenerationStatus.BUILDING,
            desired,
        )
        store = _Store(old, building)
        generations = GenerationService(store)
        operations = RAGMaintenanceOperations(
            store=store,
            backfill=_Backfill(store),
            generations=generations,
            clock=_Clock(),
            retired_generation_retention_seconds=0,
        )

        assert await operations.build_generation(desired) == building
        handoff = await operations.inspect_profile_handoff(desired)
        assert handoff.phase is RAGProfileHandoffPhase.BUILDING
        assert handoff.old_credential_cleanup_ready is False

        async with generations.pin_active_generation() as pinned:
            assert pinned.generation_id == OLD_ID
            retired = await operations.retire_generation(
                OLD_ID,
                replacement_generation_id=NEW_ID,
            )
            assert retired.status is RAGIndexGenerationStatus.RETIRED
            draining = await operations.inspect_profile_handoff(desired)
            assert draining.phase is RAGProfileHandoffPhase.READER_DRAIN
            assert draining.blocking_reader_count == 1
            with pytest.raises(
                GenerationCleanupConflictError,
                match="reader lease",
            ):
                await operations.delete_retired_generation(OLD_ID)

        ready = await operations.inspect_profile_handoff(desired)
        assert ready.phase is RAGProfileHandoffPhase.CREDENTIAL_CLEANUP_READY
        assert ready.old_credential_cleanup_ready is True
        await operations.delete_retired_generation(OLD_ID)
        assert store.deleted == [OLD_ID]

        health = await operations.inspect_index_health()
        assert health.active_generation_id == NEW_ID
        serialized = json.dumps(health.model_dump(mode="json"))
        assert "provider" not in serialized
        assert "credential" not in serialized

    asyncio.run(exercise())


def test_retry_filter_is_bounded_and_only_building_generation_is_retryable() -> None:
    async def exercise() -> None:
        old = _generation(
            OLD_ID,
            RAGIndexGenerationStatus.ACTIVE,
            _profile("embedding-old"),
        )
        building = _generation(
            NEW_ID,
            RAGIndexGenerationStatus.BUILDING,
            _profile("embedding-new"),
        )
        store = _Store(old, building)
        operations = RAGMaintenanceOperations(
            store=store,
            backfill=_Backfill(store),
            generations=GenerationService(store),
            clock=_Clock(),
            retired_generation_retention_seconds=0,
        )
        safe_filter = RAGFailedJobRetryFilter(
            safe_error_codes=("embedding_timeout",),
            limit=7,
        )
        assert await operations.retry_failed_jobs(NEW_ID, safe_filter) == ()
        assert store.retried[0][:3] == (
            NEW_ID,
            ("embedding_timeout",),
            7,
        )
        with pytest.raises(RAGMaintenanceConflictError, match="building"):
            await operations.retry_failed_jobs(OLD_ID, safe_filter)

    asyncio.run(exercise())

    with pytest.raises(ValueError, match="lowercase tokens"):
        RAGFailedJobRetryFilter(safe_error_codes=("raw provider body",))
