from typing import Protocol

from app.domain import NonEmptyString, RAGIndexJob, RAGJobCounts, UtcDatetime


class RAGJobStorePort(Protocol):
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
