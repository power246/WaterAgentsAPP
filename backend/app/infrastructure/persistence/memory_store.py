import asyncio
from uuid import UUID

from app.application.analysis_runs.creation import (
    IdempotencyKeyConflictError,
    RunCreationCandidate,
    RunCreationDisposition,
    RunCreationResult,
    StoredRunCreation,
)
from app.application.analysis_runs.models import AnalysisRunSnapshot, SessionSummary
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
)
from app.application.transcripts.models import (
    StoredTranscriptContent,
    StoredTranscriptItem,
)


class InMemoryTranscriptStore:
    """Deterministic persistence fake; callers may reuse it across app lifespans."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[UUID, RunCreationCandidate] = {}
        self._keys: dict[UUID, UUID] = {}
        self._run_snapshots: dict[UUID, AnalysisRunSnapshot] = {}
        self._sessions: dict[UUID, SessionSummary] = {}
        self._items: dict[UUID, StoredTranscriptItem] = {}
        self._contents: dict[UUID, StoredTranscriptContent] = {}
        self.write_batches: list[PersistenceWriteBatch] = []

    async def get_creation(self, idempotency_key: UUID) -> RunCreationResult | None:
        async with self._lock:
            run_id = self._keys.get(idempotency_key)
            if run_id is None:
                return None
            return self._result(self._runs[run_id], RunCreationDisposition.EXISTING)

    async def create_run(self, candidate: RunCreationCandidate) -> RunCreationResult:
        async with self._lock:
            if candidate.idempotency_key is not None:
                existing_id = self._keys.get(candidate.idempotency_key)
                if existing_id is not None:
                    existing = self._runs[existing_id]
                    if existing.request_fingerprint != candidate.request_fingerprint:
                        raise IdempotencyKeyConflictError()
                    return self._result(existing, RunCreationDisposition.EXISTING)
            if candidate.snapshot.run_id in self._runs:
                raise ValueError("run identity already exists")
            self._runs[candidate.snapshot.run_id] = candidate
            self._run_snapshots[candidate.snapshot.run_id] = candidate.snapshot
            self._items.update(
                {item.item_id: item for item in candidate.transcript.items}
            )
            self._contents.update(
                {content.item_id: content for content in candidate.transcript.contents}
            )
            if candidate.idempotency_key is not None:
                self._keys[candidate.idempotency_key] = candidate.snapshot.run_id
            return self._result(candidate, RunCreationDisposition.CREATED)

    async def verify_creation(
        self,
        candidate: RunCreationCandidate,
    ) -> RunCreationResult | None:
        async with self._lock:
            existing = self._runs.get(candidate.snapshot.run_id)
            if existing is None:
                return None
            if existing != candidate:
                raise ValueError("stored creation differs from the submitted candidate")
            return self._result(existing, RunCreationDisposition.CREATED)

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        async with self._lock:
            snapshots = dict(self._run_snapshots)
            sessions = dict(self._sessions)
            items = dict(self._items)
            contents = dict(self._contents)
            self._apply_batch(batch, snapshots, sessions, items, contents)
            self._run_snapshots = snapshots
            self._sessions = sessions
            self._items = items
            self._contents = contents
            self.write_batches.append(batch)

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        async with self._lock:
            evidence = [
                self._items.get(item.item_id) == item
                and (
                    item.inline_body is not None
                    or self._contents.get(item.item_id)
                    == next(
                        content
                        for content in batch.transcript.contents
                        if content.item_id == item.item_id
                    )
                )
                for item in batch.transcript.items
            ]
            evidence.extend(self._mutation_evidence(batch))
            if evidence and all(evidence):
                return BatchVerification.COMMITTED
            if evidence and not any(evidence):
                return BatchVerification.NOT_COMMITTED
            raise PersistenceWriteConflictError(
                "stored persistence batch is incomplete or conflicting"
            )

    def _mutation_evidence(self, batch: PersistenceWriteBatch) -> list[bool]:
        evidence: list[bool] = []
        if batch.run_mutations:
            current = self._run_snapshots.get(batch.run_id)
            evidence.append(current == batch.run_mutations[-1].snapshot)
        by_session: dict[UUID, list] = {}
        for mutation in batch.session_mutations:
            by_session.setdefault(mutation.summary.session_id, []).append(mutation)
        for session_id, mutations in by_session.items():
            evidence.append(self._sessions.get(session_id) == mutations[-1].summary)
        return evidence

    @staticmethod
    def _apply_batch(
        batch: PersistenceWriteBatch,
        snapshots: dict[UUID, AnalysisRunSnapshot],
        sessions: dict[UUID, SessionSummary],
        items: dict[UUID, StoredTranscriptItem],
        contents: dict[UUID, StoredTranscriptContent],
    ) -> None:
        for mutation in batch.run_mutations:
            current = snapshots.get(batch.run_id)
            if current is None or current.status != mutation.previous_status:
                raise PersistenceWriteConflictError("run state changed unexpectedly")
            snapshots[batch.run_id] = mutation.snapshot
        for mutation in batch.session_mutations:
            session_id = mutation.summary.session_id
            current = sessions.get(session_id)
            if mutation.previous_status is None:
                if current is not None:
                    raise PersistenceWriteConflictError(
                        "Agent session identity already exists"
                    )
            elif current is None or current.status != mutation.previous_status:
                raise PersistenceWriteConflictError(
                    "Agent session state changed unexpectedly"
                )
            sessions[session_id] = mutation.summary
        for item in batch.transcript.items:
            existing = items.get(item.item_id)
            if existing is not None and existing != item:
                raise PersistenceWriteConflictError(
                    "transcript item identity contains different content"
                )
            items[item.item_id] = item
        for content in batch.transcript.contents:
            existing = contents.get(content.item_id)
            if existing is not None and existing != content:
                raise PersistenceWriteConflictError(
                    "transcript content identity contains different content"
                )
            contents[content.item_id] = content

    @staticmethod
    def _result(
        candidate: RunCreationCandidate,
        disposition: RunCreationDisposition,
    ) -> RunCreationResult:
        return RunCreationResult(
            disposition=disposition,
            creation=StoredRunCreation(
                run_id=candidate.snapshot.run_id,
                idempotency_key=candidate.idempotency_key,
                request_fingerprint=candidate.request_fingerprint,
                creation_receipt=candidate.creation_receipt,
            ),
        )
