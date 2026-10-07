import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.application.transcript_memory import (
    GenerationCleanupConflictError,
    GenerationConfigurationError,
    GenerationService,
    GenerationStartupReadiness,
    assess_generation_startup,
    validate_desired_embedding_profile,
)
from app.domain import (
    EmbeddingProfile,
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
)

NOW = datetime(2026, 10, 8, 4, tzinfo=UTC)


def profile(*, model: str = "embedding-v1", dimensions: int = 3) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="openai",
        model=model,
        output_dimensions=dimensions,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )


def generation(
    identifier: str,
    status: RAGIndexGenerationStatus,
    *,
    embedding_profile: EmbeddingProfile | None = None,
) -> RAGIndexGeneration:
    values = {
        "generation_id": identifier,
        "profile": embedding_profile or profile(),
        "status": status,
        "source_cutoff_created_at": NOW,
        "source_cutoff_item_id": "81000000-0000-4000-8000-000000000099",
        "created_at": NOW,
    }
    if status is RAGIndexGenerationStatus.ACTIVE:
        values["activated_at"] = NOW + timedelta(seconds=1)
    elif status is RAGIndexGenerationStatus.RETIRED:
        values["activated_at"] = NOW + timedelta(seconds=1)
        values["retired_at"] = NOW + timedelta(seconds=2)
    elif status is RAGIndexGenerationStatus.FAILED:
        values["failed_at"] = NOW + timedelta(seconds=1)
        values["last_safe_error_code"] = "embedding_protocol_invalid"
    return RAGIndexGeneration.model_validate(values)


def test_desired_profile_comes_only_from_the_exclusive_catalog_entry() -> None:
    desired = profile()
    catalog = ModelCatalogSnapshot(
        capabilities=(
            ModelCapability(
                provider=desired.provider,
                model=desired.model,
                roles=(ModelRole.EMBEDDING,),
                embedding_profile=desired,
            ),
        )
    )
    assert validate_desired_embedding_profile(catalog) == desired

    with pytest.raises(GenerationConfigurationError, match="missing"):
        validate_desired_embedding_profile(ModelCatalogSnapshot(capabilities=()))


def test_startup_distinguishes_foundation_first_build_ready_and_dimension_damage() -> None:
    desired = profile()
    building = generation(
        "81000000-0000-4000-8000-000000000001",
        RAGIndexGenerationStatus.BUILDING,
        embedding_profile=desired,
    )
    active = generation(
        "81000000-0000-4000-8000-000000000002",
        RAGIndexGenerationStatus.ACTIVE,
        embedding_profile=desired,
    )

    with pytest.raises(GenerationConfigurationError, match="foundation"):
        assess_generation_startup(
            foundation_validated=False,
            desired_profile=desired,
            generations=(),
            active_projection_dimensions=(),
        )
    with pytest.raises(GenerationConfigurationError, match="missing"):
        assess_generation_startup(
            foundation_validated=True,
            desired_profile=None,
            generations=(),
            active_projection_dimensions=(),
        )

    first_build = assess_generation_startup(
        foundation_validated=True,
        desired_profile=desired,
        generations=(building,),
        active_projection_dimensions=(),
    )
    assert first_build.readiness is GenerationStartupReadiness.INDEX_BUILDING
    assert first_build.active_generation is None
    assert first_build.building_generation == building

    ready = assess_generation_startup(
        foundation_validated=True,
        desired_profile=desired,
        generations=(active,),
        active_projection_dimensions=(3,),
    )
    assert ready.readiness is GenerationStartupReadiness.READY
    assert ready.active_generation == active

    with pytest.raises(GenerationConfigurationError, match="dimensions"):
        assess_generation_startup(
            foundation_validated=True,
            desired_profile=desired,
            generations=(active,),
            active_projection_dimensions=(3, 4),
        )


def test_startup_allows_old_active_only_while_desired_building_is_consistent() -> None:
    old = generation(
        "81000000-0000-4000-8000-000000000003",
        RAGIndexGenerationStatus.ACTIVE,
        embedding_profile=profile(model="embedding-old"),
    )
    desired = profile(model="embedding-new")
    building = generation(
        "81000000-0000-4000-8000-000000000004",
        RAGIndexGenerationStatus.BUILDING,
        embedding_profile=desired,
    )
    state = assess_generation_startup(
        foundation_validated=True,
        desired_profile=desired,
        generations=(old, building),
        active_projection_dimensions=(3,),
    )
    assert state.active_generation == old
    assert state.building_generation == building

    with pytest.raises(GenerationConfigurationError, match="desired"):
        assess_generation_startup(
            foundation_validated=True,
            desired_profile=profile(model="embedding-other"),
            generations=(old, building),
            active_projection_dimensions=(3,),
        )


class MemoryGenerationStore:
    def __init__(self, active: RAGIndexGeneration) -> None:
        self.active = active
        self.generations = {active.generation_id: active}
        self.deleted: list[str] = []

    async def read_active_generation(self) -> RAGIndexGeneration | None:
        return self.active

    async def read_generation(self, generation_id: str) -> RAGIndexGeneration | None:
        return self.generations.get(generation_id)

    async def delete_generation(self, generation_id: str) -> None:
        self.deleted.append(generation_id)
        self.generations.pop(generation_id, None)


def test_reader_pins_exactly_one_generation_across_switch_and_blocks_cleanup() -> None:
    async def exercise() -> None:
        old_active = generation(
            "81000000-0000-4000-8000-000000000010",
            RAGIndexGenerationStatus.ACTIVE,
            embedding_profile=profile(model="embedding-old"),
        )
        new_active = generation(
            "81000000-0000-4000-8000-000000000011",
            RAGIndexGenerationStatus.ACTIVE,
            embedding_profile=profile(model="embedding-new"),
        )
        retired_old = generation(
            old_active.generation_id,
            RAGIndexGenerationStatus.RETIRED,
            embedding_profile=old_active.profile,
        )
        store = MemoryGenerationStore(old_active)
        service = GenerationService(store)

        async with service.pin_active_generation() as first_pin:
            store.active = new_active
            store.generations = {
                retired_old.generation_id: retired_old,
                new_active.generation_id: new_active,
            }
            async with service.pin_active_generation() as second_pin:
                assert first_pin.generation_id == old_active.generation_id
                assert second_pin.generation_id == new_active.generation_id
            with pytest.raises(GenerationCleanupConflictError, match="reader lease"):
                await service.delete_inactive_generation(
                    retired_old.generation_id,
                    visible_active_generation_id=new_active.generation_id,
                )

        with pytest.raises(GenerationCleanupConflictError, match="visibly active"):
            await service.delete_inactive_generation(
                retired_old.generation_id,
                visible_active_generation_id=old_active.generation_id,
            )
        await service.delete_inactive_generation(
            retired_old.generation_id,
            visible_active_generation_id=new_active.generation_id,
        )
        assert store.deleted == [retired_old.generation_id]

    asyncio.run(exercise())
