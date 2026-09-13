from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

CreationCandidateT = TypeVar("CreationCandidateT")
CreationResultT = TypeVar("CreationResultT")
WriteBatchT = TypeVar("WriteBatchT")
VerificationT = TypeVar("VerificationT")
ReadRunT = TypeVar("ReadRunT", covariant=True)
ReadHistoryPageT = TypeVar("ReadHistoryPageT", covariant=True)


class TranscriptStorePort(
    Protocol[
        CreationCandidateT,
        CreationResultT,
        WriteBatchT,
        VerificationT,
    ]
):
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

    async def write_batch(self, batch: WriteBatchT) -> None: ...

    async def verify_batch(self, batch: WriteBatchT) -> VerificationT: ...


class TranscriptReadStorePort(Protocol[ReadRunT, ReadHistoryPageT]):
    """Persistence boundary for consistent transcript history snapshots."""

    async def list_runs(
        self,
        limit: int,
        after_created_at: datetime | None,
        after_run_id: UUID | None,
    ) -> ReadHistoryPageT: ...

    async def read_run(self, run_id: UUID) -> ReadRunT | None: ...
