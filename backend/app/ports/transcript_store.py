from typing import Protocol, TypeVar
from uuid import UUID

CreationCandidateT = TypeVar("CreationCandidateT")
CreationResultT = TypeVar("CreationResultT")


class TranscriptStorePort(Protocol[CreationCandidateT, CreationResultT]):
    """Persistence boundary for atomic run creation and later transcript writes."""

    async def get_creation(
        self,
        idempotency_key: UUID,
    ) -> CreationResultT | None: ...

    async def create_run(self, candidate: CreationCandidateT) -> CreationResultT: ...

    async def verify_creation(
        self,
        candidate: CreationCandidateT,
    ) -> CreationResultT | None: ...
