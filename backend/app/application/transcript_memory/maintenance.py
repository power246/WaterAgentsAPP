from __future__ import annotations

from datetime import timedelta
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.transcript_memory.backfill import RAGBackfillCoordinator
from app.application.transcript_memory.generations import GenerationService
from app.domain import (
    EmbeddingProfile,
    RAGCleanupPlan,
    RAGCleanupResult,
    RAGFailedJobRetryFilter,
    RAGGenerationHealth,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexHealth,
    RAGIndexJob,
    RAGJobCounts,
    RAGProfileHandoffPhase,
    RAGProfileHandoffState,
    UtcDatetime,
)
from app.ports import ClockPort


class RAGMaintenanceConflictError(RuntimeError):
    """An internal maintenance request would violate persisted ownership."""


@dataclass(frozen=True, slots=True)
class HistoryStorageFacts:
    run_ids: tuple[str, ...]
    sessions: int
    transcript_items: int
    idempotency_keys: int
    all_terminal: bool


class RAGMaintenanceStore(Protocol):
    async def list_history_run_ids(self, *, limit: int) -> tuple[str, ...]: ...

    async def inspect_history_storage(self, run_ids: tuple[str, ...]) -> HistoryStorageFacts: ...

    async def failed_job_codes(self, generation_id: str) -> tuple[str, ...]: ...

    async def count_failed_jobs(
        self, generation_id: str, safe_error_codes: tuple[str, ...],
    ) -> int: ...
    async def list_generations(self) -> tuple[RAGIndexGeneration, ...]: ...

    async def read_generation(
        self,
        generation_id: str,
    ) -> RAGIndexGeneration | None: ...

    async def read_active_generation(self) -> RAGIndexGeneration | None: ...

    async def counts(self, generation_id: str) -> RAGJobCounts: ...

    async def activate_generation(
        self,
        generation_id: str,
        activated_at: UtcDatetime,
    ) -> RAGIndexGeneration: ...

    async def retry_failed_jobs(
        self,
        generation_id: str,
        safe_error_codes: tuple[str, ...],
        limit: int,
        retried_at: UtcDatetime,
    ) -> tuple[RAGIndexJob, ...]: ...

    async def inspect_cleanup_dependencies(
        self,
        run_ids: tuple[str, ...],
    ) -> RAGCleanupPlan: ...

    async def delete_history_runs(
        self,
        run_ids: tuple[str, ...],
        *,
        cascade_consumers: bool,
        expected_delete_run_ids: tuple[str, ...] | None = None,
        expected_dependency_ids: tuple[str, ...] | None = None,
    ) -> RAGCleanupResult: ...


class RAGMaintenanceOperations:
    """Typed internal operations; authorization and HTTP exposure stay deferred."""

    def __init__(
        self,
        *,
        store: RAGMaintenanceStore,
        backfill: RAGBackfillCoordinator,
        generations: GenerationService,
        clock: ClockPort,
        retired_generation_retention_seconds: float,
    ) -> None:
        if retired_generation_retention_seconds < 0:
            raise ValueError("retired generation retention must not be negative")
        self._store = store
        self._backfill = backfill
        self._generations = generations
        self._clock = clock
        self._retention = timedelta(
            seconds=retired_generation_retention_seconds
        )

    async def inspect_index_health(self) -> RAGIndexHealth:
        generations = await self._store.list_generations()
        active_generation_id = next(
            (
                item.generation_id
                for item in generations
                if item.status is RAGIndexGenerationStatus.ACTIVE
            ),
            None,
        )
        health: list[RAGGenerationHealth] = []
        for generation in generations:
            health.append(
                RAGGenerationHealth(
                    generation_id=generation.generation_id,
                    status=generation.status,
                    profile_digest=generation.profile.profile_digest,
                    job_counts=await self._store.counts(
                        generation.generation_id
                    ),
                    reader_count=await self._generations.reader_count(
                        generation.generation_id
                    ),
                    created_at=generation.created_at,
                    activated_at=generation.activated_at,
                    retired_at=generation.retired_at,
                    failed_at=generation.failed_at,
                    last_safe_error_code=generation.last_safe_error_code,
                )
            )
        return RAGIndexHealth(
            active_generation_id=active_generation_id,
            generations=tuple(health),
        )

    async def build_generation(
        self,
        profile: EmbeddingProfile,
    ) -> RAGIndexGeneration:
        building = await self._backfill.ensure_building_generation(profile)
        if building is not None:
            return building
        active = await self._store.read_active_generation()
        if active is None or active.profile != profile:
            raise RAGMaintenanceConflictError(
                "desired profile has neither a building nor active generation"
            )
        return active

    async def retry_failed_jobs(
        self,
        generation_id: str,
        safe_filter: RAGFailedJobRetryFilter,
    ) -> tuple[RAGIndexJob, ...]:
        generation = await self._require_generation(generation_id)
        if generation.status is not RAGIndexGenerationStatus.BUILDING:
            raise RAGMaintenanceConflictError(
                "failed jobs can only be retried for a building generation"
            )
        return await self._store.retry_failed_jobs(
            generation_id,
            safe_filter.safe_error_codes,
            safe_filter.limit,
            self._clock.now(),
        )

    async def retire_generation(
        self,
        generation_id: str,
        *,
        replacement_generation_id: str,
    ) -> RAGIndexGeneration:
        target = await self._require_generation(generation_id)
        replacement = await self._require_generation(replacement_generation_id)
        if target.status is RAGIndexGenerationStatus.RETIRED:
            if replacement.status is RAGIndexGenerationStatus.ACTIVE:
                return target
            raise RAGMaintenanceConflictError(
                "retired generation has no visibly active replacement"
            )
        if target.status is not RAGIndexGenerationStatus.ACTIVE:
            raise RAGMaintenanceConflictError(
                "only the active generation can be replaced"
            )
        if replacement.status is not RAGIndexGenerationStatus.BUILDING:
            raise RAGMaintenanceConflictError(
                "replacement generation must still be building"
            )
        await self._store.activate_generation(
            replacement_generation_id,
            self._clock.now(),
        )
        retired = await self._store.read_generation(generation_id)
        if retired is None or retired.status is not RAGIndexGenerationStatus.RETIRED:
            raise RAGMaintenanceConflictError(
                "generation replacement did not atomically retire the old active"
            )
        return retired

    async def delete_retired_generation(self, generation_id: str) -> None:
        generation = await self._store.read_generation(generation_id)
        if generation is None:
            return
        if generation.status not in {
            RAGIndexGenerationStatus.RETIRED,
            RAGIndexGenerationStatus.FAILED,
        }:
            raise RAGMaintenanceConflictError(
                "only retired or failed generations can be deleted"
            )
        if (
            generation.status is RAGIndexGenerationStatus.RETIRED
            and generation.retired_at is not None
            and self._clock.now() < generation.retired_at + self._retention
        ):
            raise RAGMaintenanceConflictError(
                "retired generation retention period has not elapsed"
            )
        active = await self._store.read_active_generation()
        await self._generations.delete_inactive_generation(
            generation_id,
            visible_active_generation_id=(
                None if active is None else active.generation_id
            ),
        )

    async def inspect_generation_deletion(self, generation_id: str) -> RAGIndexGeneration:
        generation = await self._require_generation(generation_id)
        if generation.status not in {
            RAGIndexGenerationStatus.RETIRED,
            RAGIndexGenerationStatus.FAILED,
        }:
            raise RAGMaintenanceConflictError(
                "only retired or failed generations can be deleted"
            )
        if (
            generation.status is RAGIndexGenerationStatus.RETIRED
            and generation.retired_at is not None
            and self._clock.now() < generation.retired_at + self._retention
        ):
            raise RAGMaintenanceConflictError(
                "retired generation retention period has not elapsed"
            )
        active = await self._store.read_active_generation()
        if active is None or active.generation_id == generation_id:
            raise RAGMaintenanceConflictError("a visible active replacement is required")
        if await self._generations.reader_count(generation_id):
            raise RAGMaintenanceConflictError("generation readers must drain")
        return generation

    async def list_history_run_ids(self, *, limit: int = 501) -> tuple[str, ...]:
        return await self._store.list_history_run_ids(limit=limit)

    async def inspect_history_storage(self, run_ids: tuple[str, ...]) -> HistoryStorageFacts:
        return await self._store.inspect_history_storage(run_ids)

    async def failed_job_codes(self, generation_id: str) -> tuple[str, ...]:
        return await self._store.failed_job_codes(generation_id)

    async def count_failed_jobs(
        self, generation_id: str, safe_error_codes: tuple[str, ...],
    ) -> int:
        return await self._store.count_failed_jobs(generation_id, safe_error_codes)

    async def inspect_cleanup_dependencies(
        self,
        run_ids: tuple[str, ...],
    ) -> RAGCleanupPlan:
        return await self._store.inspect_cleanup_dependencies(
            _canonical_run_ids(run_ids)
        )

    async def delete_history_runs(
        self,
        run_ids: tuple[str, ...],
        *,
        cascade_consumers: bool = False,
        expected_delete_run_ids: tuple[str, ...] | None = None,
        expected_dependency_ids: tuple[str, ...] | None = None,
    ) -> RAGCleanupResult:
        options = {"cascade_consumers": cascade_consumers}
        if expected_delete_run_ids is not None:
            options["expected_delete_run_ids"] = expected_delete_run_ids
            options["expected_dependency_ids"] = expected_dependency_ids
        return await self._store.delete_history_runs(_canonical_run_ids(run_ids), **options)

    async def inspect_profile_handoff(
        self,
        desired_profile: EmbeddingProfile,
    ) -> RAGProfileHandoffState:
        generations = await self._store.list_generations()
        active = next(
            (
                item
                for item in generations
                if item.status is RAGIndexGenerationStatus.ACTIVE
            ),
            None,
        )
        building = next(
            (
                item
                for item in generations
                if item.status is RAGIndexGenerationStatus.BUILDING
                and item.profile == desired_profile
            ),
            None,
        )
        retired_old = tuple(
            item
            for item in generations
            if item.status is RAGIndexGenerationStatus.RETIRED
            and item.profile.profile_digest != desired_profile.profile_digest
        )
        if active is not None and active.profile == desired_profile:
            blocking = sum(
                [
                    await self._generations.reader_count(item.generation_id)
                    for item in retired_old
                ]
            )
            phase = (
                RAGProfileHandoffPhase.READER_DRAIN
                if blocking
                else RAGProfileHandoffPhase.CREDENTIAL_CLEANUP_READY
            )
            return RAGProfileHandoffState(
                desired_profile_digest=desired_profile.profile_digest,
                phase=phase,
                active_generation_id=active.generation_id,
                building_generation_id=(
                    None if building is None else building.generation_id
                ),
                retired_generation_ids=tuple(
                    item.generation_id for item in retired_old
                ),
                blocking_reader_count=blocking,
                old_credential_cleanup_ready=(
                    phase is RAGProfileHandoffPhase.CREDENTIAL_CLEANUP_READY
                ),
            )
        return RAGProfileHandoffState(
            desired_profile_digest=desired_profile.profile_digest,
            phase=(
                RAGProfileHandoffPhase.BUILDING
                if building is not None
                else RAGProfileHandoffPhase.BUILD_REQUIRED
            ),
            active_generation_id=(
                None if active is None else active.generation_id
            ),
            building_generation_id=(
                None if building is None else building.generation_id
            ),
            retired_generation_ids=tuple(
                item.generation_id for item in retired_old
            ),
        )

    async def _require_generation(
        self,
        generation_id: str,
    ) -> RAGIndexGeneration:
        generation = await self._store.read_generation(generation_id)
        if generation is None:
            raise RAGMaintenanceConflictError("generation does not exist")
        return generation


def _canonical_run_ids(run_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not run_ids:
        raise ValueError("run_ids must not be empty")
    canonical = tuple(str(UUID(value)) for value in run_ids)
    if len(canonical) != len(set(canonical)):
        raise ValueError("run_ids must not contain duplicates")
    return canonical
