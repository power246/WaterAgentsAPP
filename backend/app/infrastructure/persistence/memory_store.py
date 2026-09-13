import asyncio
from uuid import UUID

from app.application.analysis_runs.creation import (
    IdempotencyKeyConflictError,
    RunCreationCandidate,
    RunCreationDisposition,
    RunCreationResult,
    StoredRunCreation,
)


class InMemoryTranscriptStore:
    """Deterministic persistence fake; callers may reuse it across app lifespans."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[UUID, RunCreationCandidate] = {}
        self._keys: dict[UUID, UUID] = {}

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
