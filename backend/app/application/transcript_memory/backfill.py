from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.transcript_memory.discovery import (
    RAGDiscoveryResult,
    RAGDiscoveryService,
)
from app.domain import (
    EmbeddingProfile,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    UtcDatetime,
)


class RAGGenerationBackfillStore(Protocol):
    async def read_active_generation(self) -> RAGIndexGeneration | None: ...

    async def list_generations(self) -> tuple[RAGIndexGeneration, ...]: ...

    async def create_building_generation(
        self,
        profile: EmbeddingProfile,
        source_cutoff_created_at: UtcDatetime,
        source_cutoff_item_id: str,
    ) -> RAGIndexGeneration: ...

    async def activate_generation(
        self,
        generation_id: str,
        activated_at: UtcDatetime,
    ) -> RAGIndexGeneration: ...


@dataclass(frozen=True, slots=True)
class RAGBackfillReconciliation:
    active: RAGDiscoveryResult | None
    building: RAGDiscoveryResult | None


class RAGBackfillCoordinator:
    """Coordinates cutoff creation and discovery; PostgreSQL owns activation proof."""

    def __init__(
        self,
        store: RAGGenerationBackfillStore,
        discovery: RAGDiscoveryService,
        *,
        max_discovery_batches: int,
    ) -> None:
        if type(max_discovery_batches) is not int or max_discovery_batches < 1:
            raise ValueError("max_discovery_batches must be a positive integer")
        self._store = store
        self._discovery = discovery
        self._max_discovery_batches = max_discovery_batches

    async def ensure_building_generation(
        self,
        profile: EmbeddingProfile,
    ) -> RAGIndexGeneration | None:
        generations = await self._store.list_generations()
        building = next(
            (
                item
                for item in generations
                if item.status is RAGIndexGenerationStatus.BUILDING
            ),
            None,
        )
        if building is not None:
            if building.profile != profile:
                raise ValueError("building generation profile differs from desired profile")
            return building
        active = next(
            (
                item
                for item in generations
                if item.status is RAGIndexGenerationStatus.ACTIVE
            ),
            None,
        )
        if active is not None and active.profile == profile:
            return None
        cutoff = await self._discovery.latest_cutoff()
        return await self._store.create_building_generation(
            profile,
            cutoff.created_at,
            str(cutoff.item_id),
        )

    async def reconcile(
        self,
        *,
        discovered_at: UtcDatetime,
    ) -> RAGBackfillReconciliation:
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
            ),
            None,
        )
        active_result = (
            await self._discovery.reconcile(
                active,
                discovered_at=discovered_at,
                max_batches=self._max_discovery_batches,
            )
            if active is not None
            else None
        )
        building_result = (
            await self._discovery.reconcile(
                building,
                discovered_at=discovered_at,
                max_batches=self._max_discovery_batches,
            )
            if building is not None
            else None
        )
        return RAGBackfillReconciliation(active=active_result, building=building_result)

    async def activate_when_complete(
        self,
        generation: RAGIndexGeneration,
        *,
        activated_at: UtcDatetime,
    ) -> RAGIndexGeneration:
        discovered = await self._discovery.reconcile(
            generation,
            discovered_at=activated_at,
            max_batches=self._max_discovery_batches,
        )
        if not discovered.exhausted:
            raise RuntimeError("generation cutoff discovery is not exhausted")
        activated = await self._store.activate_generation(
            generation.generation_id,
            activated_at,
        )
        # The same query immediately catches cutoff-after sources; periodic calls
        # remain authoritative if this bounded hint is interrupted.
        await self._discovery.reconcile(
            activated,
            discovered_at=activated_at,
            max_batches=self._max_discovery_batches,
        )
        return activated
