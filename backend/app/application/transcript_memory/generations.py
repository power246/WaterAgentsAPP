from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from pydantic import ValidationError

from app.domain import (
    EmbeddingProfile,
    ModelCatalogSnapshot,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
)


class GenerationConfigurationError(RuntimeError):
    """The desired profile or persisted generation state is not safe to use."""


class GenerationUnavailableError(RuntimeError):
    """No active generation can be pinned for a search operation."""


class GenerationCleanupConflictError(RuntimeError):
    """An inactive generation is not yet safe to delete."""


class GenerationStartupReadiness(Enum):
    READY = "ready"
    INDEX_BUILDING = "index_building"


@dataclass(frozen=True, slots=True)
class GenerationStartupState:
    readiness: GenerationStartupReadiness
    active_generation: RAGIndexGeneration | None
    building_generation: RAGIndexGeneration | None


class GenerationStore(Protocol):
    async def read_active_generation(self) -> RAGIndexGeneration | None: ...

    async def read_generation(
        self,
        generation_id: str,
    ) -> RAGIndexGeneration | None: ...

    async def delete_generation(self, generation_id: str) -> None: ...


def validate_desired_embedding_profile(
    catalog: ModelCatalogSnapshot,
) -> EmbeddingProfile:
    try:
        profile = catalog.embedding_profile
    except StopIteration as error:
        raise GenerationConfigurationError(
            "model catalog is missing the desired embedding profile"
        ) from error
    _validate_profile_integrity(profile)
    return profile


def assess_generation_startup(
    *,
    foundation_validated: bool,
    desired_profile: EmbeddingProfile | None,
    generations: tuple[RAGIndexGeneration, ...],
    active_projection_dimensions: tuple[int, ...],
) -> GenerationStartupState:
    if not foundation_validated:
        raise GenerationConfigurationError(
            "RAG database schema or vector foundation is not validated"
        )
    if desired_profile is None:
        raise GenerationConfigurationError(
            "model catalog is missing the desired embedding profile"
        )
    _validate_profile_integrity(desired_profile)
    for generation in generations:
        _validate_generation_integrity(generation)

    active = tuple(
        item
        for item in generations
        if item.status is RAGIndexGenerationStatus.ACTIVE
    )
    building = tuple(
        item
        for item in generations
        if item.status is RAGIndexGenerationStatus.BUILDING
    )
    if len(active) > 1 or len(building) > 1:
        raise GenerationConfigurationError(
            "persisted generation cardinality violates the active/building contract"
        )

    active_generation = active[0] if active else None
    building_generation = building[0] if building else None
    if building_generation is not None and (
        building_generation.profile.profile_id != desired_profile.profile_id
        or building_generation.profile.profile_digest != desired_profile.profile_digest
    ):
        raise GenerationConfigurationError(
            "building generation does not use the desired embedding profile"
        )

    if active_generation is None:
        if building_generation is None:
            raise GenerationConfigurationError(
                "no active or legitimate first building generation exists"
            )
        if active_projection_dimensions:
            raise GenerationConfigurationError(
                "projection dimensions exist without an active generation"
            )
        return GenerationStartupState(
            readiness=GenerationStartupReadiness.INDEX_BUILDING,
            active_generation=None,
            building_generation=building_generation,
        )

    expected_dimensions = active_generation.profile.output_dimensions
    if any(value != expected_dimensions for value in active_projection_dimensions):
        raise GenerationConfigurationError(
            "active generation projection dimensions do not match its profile"
        )
    return GenerationStartupState(
        readiness=GenerationStartupReadiness.READY,
        active_generation=active_generation,
        building_generation=building_generation,
    )


class GenerationService:
    """Pins one generation per reader and guards later M9 cleanup operations."""

    def __init__(self, store: GenerationStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._reader_counts: dict[str, int] = {}

    @asynccontextmanager
    async def pin_active_generation(self) -> AsyncIterator[RAGIndexGeneration]:
        async with self._lock:
            generation = await self._store.read_active_generation()
            if generation is None:
                raise GenerationUnavailableError(
                    "historical index has no active generation"
                )
            _validate_generation_integrity(generation)
            identifier = generation.generation_id
            self._reader_counts[identifier] = self._reader_counts.get(identifier, 0) + 1
        try:
            yield generation
        finally:
            async with self._lock:
                remaining = self._reader_counts[identifier] - 1
                if remaining:
                    self._reader_counts[identifier] = remaining
                else:
                    del self._reader_counts[identifier]

    async def delete_inactive_generation(
        self,
        generation_id: str,
        *,
        visible_active_generation_id: str | None,
    ) -> None:
        async with self._lock:
            if self._reader_counts.get(generation_id, 0):
                raise GenerationCleanupConflictError(
                    "generation still has an active reader lease"
                )
            target = await self._store.read_generation(generation_id)
            if target is None:
                return
            if target.status not in {
                RAGIndexGenerationStatus.RETIRED,
                RAGIndexGenerationStatus.FAILED,
            }:
                raise GenerationCleanupConflictError(
                    "only inactive generations can be deleted"
                )
            if target.status is RAGIndexGenerationStatus.RETIRED:
                active = await self._store.read_active_generation()
                if (
                    active is None
                    or visible_active_generation_id != active.generation_id
                    or active.generation_id == generation_id
                ):
                    raise GenerationCleanupConflictError(
                        "the replacement generation is not yet visibly active"
                    )
            await self._store.delete_generation(generation_id)

    async def reader_count(self, generation_id: str) -> int:
        """Return the process-local leases guarded by this service instance."""

        async with self._lock:
            return self._reader_counts.get(generation_id, 0)


def _validate_profile_integrity(profile: EmbeddingProfile) -> None:
    try:
        validated = EmbeddingProfile.model_validate(profile.model_dump(mode="python"))
    except ValidationError as error:
        raise GenerationConfigurationError(
            "embedding profile identity is invalid"
        ) from error
    if validated != profile:
        raise GenerationConfigurationError("embedding profile identity is invalid")


def _validate_generation_integrity(generation: RAGIndexGeneration) -> None:
    _validate_profile_integrity(generation.profile)
    try:
        validated = RAGIndexGeneration.model_validate(
            generation.model_dump(mode="python")
        )
    except ValidationError as error:
        raise GenerationConfigurationError(
            "persisted generation state is invalid"
        ) from error
    if validated != generation:
        raise GenerationConfigurationError("persisted generation state is invalid")
