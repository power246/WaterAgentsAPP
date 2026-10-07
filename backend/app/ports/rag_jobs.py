from typing import Protocol

from app.domain import (
    NonEmptyString,
    RAGIndexJob,
    RAGJobCounts,
    RAGSourceCutoff,
    UtcDatetime,
)


class RAGJobStorePort(Protocol):
    async def latest_candidate_cutoff(self) -> RAGSourceCutoff: ...

    async def discover_jobs(
        self,
        generation_id: NonEmptyString,
        *,
        through: RAGSourceCutoff | None,
        limit: int,
        discovered_at: UtcDatetime,
    ) -> tuple[RAGIndexJob, ...]: ...

    async def enqueue(self, job: RAGIndexJob) -> RAGIndexJob: ...

    async def claim(
        self,
        generation_id: NonEmptyString,
        lease_owner: NonEmptyString,
        lease_until: UtcDatetime,
        limit: int,
    ) -> tuple[RAGIndexJob, ...]: ...

    async def save(self, job: RAGIndexJob) -> None: ...

    async def counts(self, generation_id: NonEmptyString) -> RAGJobCounts: ...
