import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.application.transcript_memory import (
    RAGBackfillCoordinator,
    RAGDiscoveryService,
)
from app.domain import (
    EmbeddingProfile,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGSourceCutoff,
)


NOW = datetime(2026, 10, 8, 10, tzinfo=UTC)
CUTOFF_ID = UUID("87000000-0000-4000-8000-000000000001")


class _Store:
    def __init__(self, active=None) -> None:
        self.generations = [] if active is None else [active]
        self.discoveries: list[tuple[str, RAGSourceCutoff | None]] = []
        self.pending_batches: dict[str, int] = {}

    async def latest_candidate_cutoff(self):
        return RAGSourceCutoff(created_at=NOW, item_id=CUTOFF_ID)

    async def list_generations(self):
        return tuple(self.generations)

    async def read_active_generation(self):
        return next(
            (item for item in self.generations if item.status is RAGIndexGenerationStatus.ACTIVE),
            None,
        )

    async def create_building_generation(self, profile, created_at, item_id):
        generation = _generation(
            profile,
            RAGIndexGenerationStatus.BUILDING,
            suffix=2,
            cutoff_created_at=created_at,
            cutoff_item_id=item_id,
        )
        self.generations.append(generation)
        return generation

    async def discover_jobs(self, generation_id, *, through, limit, discovered_at):
        self.discoveries.append((generation_id, through))
        remaining = self.pending_batches.get(generation_id, 0)
        if remaining:
            self.pending_batches[generation_id] = remaining - 1
            return tuple(object() for _ in range(limit))
        return ()

    async def activate_generation(self, generation_id, activated_at):
        building = next(item for item in self.generations if item.generation_id == generation_id)
        active = next(
            (item for item in self.generations if item.status is RAGIndexGenerationStatus.ACTIVE),
            None,
        )
        if active is not None:
            self.generations.remove(active)
            self.generations.append(
                active.model_copy(
                    update={
                        "status": RAGIndexGenerationStatus.RETIRED,
                        "retired_at": activated_at,
                    }
                )
            )
        self.generations.remove(building)
        activated = building.model_copy(
            update={
                "status": RAGIndexGenerationStatus.ACTIVE,
                "activated_at": activated_at,
            }
        )
        self.generations.append(activated)
        return activated


def _profile(model: str = "new") -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="test",
        model=model,
        output_dimensions=3,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )


def _generation(
    profile,
    status,
    *,
    suffix,
    cutoff_created_at=NOW - timedelta(days=1),
    cutoff_item_id=str(CUTOFF_ID),
):
    return RAGIndexGeneration(
        generation_id=f"87000000-0000-4000-8000-{suffix:012d}",
        profile=profile,
        status=status,
        source_cutoff_created_at=cutoff_created_at,
        source_cutoff_item_id=cutoff_item_id,
        created_at=NOW - timedelta(days=1),
        activated_at=(NOW - timedelta(hours=1))
        if status is RAGIndexGenerationStatus.ACTIVE
        else None,
    )


def test_first_generation_captures_stable_cutoff_and_activation_catches_delta() -> None:
    async def exercise() -> None:
        store = _Store()
        coordinator = RAGBackfillCoordinator(
            store,
            RAGDiscoveryService(store, batch_size=2),
            max_discovery_batches=2,
        )
        building = await coordinator.ensure_building_generation(_profile())
        assert building is not None
        assert building.source_cutoff_created_at == NOW
        assert building.source_cutoff_item_id == str(CUTOFF_ID)

        activated = await coordinator.activate_when_complete(
            building,
            activated_at=NOW + timedelta(minutes=1),
        )
        assert activated.status is RAGIndexGenerationStatus.ACTIVE
        assert store.discoveries[0][1] is not None
        assert store.discoveries[-1][1] is None

    asyncio.run(exercise())


def test_profile_change_keeps_old_active_reconciled_while_building_from_source() -> None:
    async def exercise() -> None:
        old = _generation(_profile("old"), RAGIndexGenerationStatus.ACTIVE, suffix=1)
        store = _Store(old)
        coordinator = RAGBackfillCoordinator(
            store,
            RAGDiscoveryService(store, batch_size=2),
            max_discovery_batches=1,
        )
        building = await coordinator.ensure_building_generation(_profile("new"))
        assert building is not None

        await coordinator.reconcile(discovered_at=NOW)
        by_generation = {generation_id: through for generation_id, through in store.discoveries}
        assert by_generation[old.generation_id] is None
        assert by_generation[building.generation_id] is not None
        assert (await store.read_active_generation()).generation_id == old.generation_id

    asyncio.run(exercise())


def test_activation_refuses_when_bounded_cutoff_discovery_is_not_exhausted() -> None:
    async def exercise() -> None:
        store = _Store()
        coordinator = RAGBackfillCoordinator(
            store,
            RAGDiscoveryService(store, batch_size=1),
            max_discovery_batches=1,
        )
        building = await coordinator.ensure_building_generation(_profile())
        assert building is not None
        store.pending_batches[building.generation_id] = 2

        with pytest.raises(RuntimeError, match="not exhausted"):
            await coordinator.activate_when_complete(building, activated_at=NOW)
        assert await store.read_active_generation() is None

    asyncio.run(exercise())
