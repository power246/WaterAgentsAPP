from typing import Protocol

from app.domain import (
    AuthorizedRAGSearchQuery,
    EmbeddingProfile,
    NonEmptyString,
    RAGCandidateRow,
    RAGCaseProjectionV1,
    RAGFragmentProjectionV1,
    RAGIndexGeneration,
    RAGIndexJob,
    UtcDatetime,
)


class RAGIndexPort(Protocol):
    """Generation-scoped index access; empty candidates mean no match."""

    async def write_ready_projection(
        self,
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None: ...

    async def search_candidates(
        self,
        query: AuthorizedRAGSearchQuery,
    ) -> tuple[RAGCandidateRow, ...]: ...

    async def read_active_generation(self) -> RAGIndexGeneration | None: ...

    async def create_building_generation(
        self,
        profile: EmbeddingProfile,
        source_cutoff_created_at: UtcDatetime,
        source_cutoff_item_id: NonEmptyString,
    ) -> RAGIndexGeneration: ...

    async def activate_generation(
        self,
        generation_id: NonEmptyString,
        activated_at: UtcDatetime,
    ) -> RAGIndexGeneration: ...

    async def retire_generation(
        self,
        generation_id: NonEmptyString,
        retired_at: UtcDatetime,
    ) -> RAGIndexGeneration: ...

    async def delete_generation(self, generation_id: NonEmptyString) -> None: ...
