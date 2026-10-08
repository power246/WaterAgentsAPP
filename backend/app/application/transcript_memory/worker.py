from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol

from app.application.transcript_memory.embedding_contract import (
    EmbeddingContractError,
    validated_embedding_batch,
)
from app.application.transcript_memory.projection_builder import (
    PreparedRAGProjectionBundle,
    RAGProjectionBuildError,
    build_prepared_projection,
)
from app.application.transcript_memory.source_closure import RAGSourceClosureError
from app.application.transcript_memory.source_models import RAGCaseSource
from app.application.transcript_memory.source_reader import (
    RAGSourceReadError,
    RAGSourceUnavailableError,
)
from app.domain import (
    RAGIndexGeneration,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGCaseProjectionV1,
    RAGFragmentProjectionV1,
    UtcDatetime,
)
from app.ports import (
    ClockPort,
    EmbeddingCancelledError,
    EmbeddingPort,
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
)

_EXCLUDED_SOURCE_CODES = {
    "candidate_not_marked",
    "lifecycle_not_completed",
    "persistence_unconfirmed",
    "historical_source_not_rag_candidate",
}


class RAGWorkerStore(Protocol):
    async def claim(
        self,
        generation_id: str,
        lease_owner: str,
        lease_until: UtcDatetime,
        limit: int,
    ) -> tuple[RAGIndexJob, ...]: ...

    async def save(self, job: RAGIndexJob) -> None: ...

    async def write_ready_projection(
        self,
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None: ...


class RAGCaseSourceLoader(Protocol):
    async def load_case_source(self, job: RAGIndexJob) -> RAGCaseSource: ...


class _StopToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request(self) -> None:
        self._event.set()

    def is_requested(self) -> bool:
        return self._event.is_set()


class RAGIndexWorker:
    """Bounded generation worker; it never owns run or session lifecycle state."""

    def __init__(
        self,
        *,
        store: RAGWorkerStore,
        source_loader: RAGCaseSourceLoader,
        embedding: EmbeddingPort | None,
        embedding_factory: Callable[[], Awaitable[EmbeddingPort]] | None = None,
        clock: ClockPort,
        lease_owner: str,
        claim_batch_size: int,
        embedding_batch_size: int,
        max_concurrency: int,
        lease_seconds: float,
        attempt_timeout_seconds: float,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        integer_limits = (claim_batch_size, embedding_batch_size, max_concurrency)
        if any(type(value) is not int or value < 1 for value in integer_limits):
            raise ValueError("worker batch sizes must be positive integers")
        if embedding_batch_size > claim_batch_size:
            raise ValueError("embedding batch size cannot exceed claim batch size")
        if not lease_owner.strip():
            raise ValueError("worker lease_owner must not be blank")
        if any(
            value <= 0
            for value in (
                lease_seconds,
                attempt_timeout_seconds,
                backoff_base_seconds,
                backoff_max_seconds,
            )
        ):
            raise ValueError("worker durations must be positive")
        if lease_seconds <= attempt_timeout_seconds:
            raise ValueError("job lease must outlive one attempt deadline")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("maximum backoff cannot be below base backoff")
        self._store = store
        self._source_loader = source_loader
        self._embedding = embedding
        self._embedding_factory = embedding_factory
        if embedding is None and embedding_factory is None:
            raise ValueError("worker embedding port is required")
        self._clock = clock
        self._lease_owner = lease_owner
        self._claim_batch_size = claim_batch_size
        self._embedding_batch_size = embedding_batch_size
        self._max_concurrency = max_concurrency
        self._lease_seconds = lease_seconds
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._monotonic = monotonic
        self._stop = _StopToken()
        self._inflight: set[asyncio.Task[object]] = set()

    def request_stop(self) -> None:
        self._stop.request()

    async def run_once(self, generation: RAGIndexGeneration) -> int:
        if self._stop.is_requested():
            return 0
        now = self._clock.now()
        jobs = await self._store.claim(
            generation.generation_id,
            self._lease_owner,
            now + timedelta(seconds=self._lease_seconds),
            self._claim_batch_size,
        )
        if not jobs:
            return 0
        task = asyncio.create_task(self._process_claimed(generation, jobs))
        self._inflight.add(task)
        try:
            await task
        finally:
            self._inflight.discard(task)
        return len(jobs)

    async def run_available(self, generation: RAGIndexGeneration) -> int:
        """Claim/process at most one batch per configured concurrent lane."""

        if self._stop.is_requested():
            return 0
        processed = await asyncio.gather(
            *(self.run_once(generation) for _ in range(self._max_concurrency))
        )
        return sum(processed)

    async def drain(self, timeout_seconds: float) -> bool:
        self.request_stop()
        if not self._inflight:
            return True
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.shield(asyncio.gather(*tuple(self._inflight)))
        except TimeoutError:
            return False
        return True

    async def _process_claimed(
        self,
        generation: RAGIndexGeneration,
        jobs: tuple[RAGIndexJob, ...],
    ) -> None:
        prepared: list[tuple[RAGIndexJob, PreparedRAGProjectionBundle]] = []
        for job in jobs:
            if self._stop.is_requested():
                return
            try:
                source = await self._source_loader.load_case_source(job)
                bundle = build_prepared_projection(
                    source,
                    generation_id=generation.generation_id,
                    profile_id=generation.profile.profile_id,
                )
            except (RAGSourceClosureError, RAGSourceReadError) as error:
                code = getattr(error, "code", "source_invalid")
                if code in _EXCLUDED_SOURCE_CODES:
                    await self._terminal(
                        job,
                        RAGIndexJobStatus.EXCLUDED,
                        "source_no_longer_eligible",
                    )
                else:
                    await self._terminal(
                        job,
                        RAGIndexJobStatus.FAILED,
                        self._safe_code(error),
                    )
            except RAGSourceUnavailableError:
                await self._retry_or_fail(
                    job,
                    "source_temporarily_unavailable",
                    retryable=True,
                )
            except (RAGProjectionBuildError, ValueError) as error:
                await self._terminal(
                    job,
                    RAGIndexJobStatus.FAILED,
                    self._safe_code(error),
                )
            else:
                prepared.append((job, bundle))

        for offset in range(0, len(prepared), self._embedding_batch_size):
            if self._stop.is_requested():
                return
            chunk = prepared[offset : offset + self._embedding_batch_size]
            await self._embed_and_write(generation, chunk)

    async def _embed_and_write(self, generation, chunk) -> None:
        texts = tuple(bundle.case.canonical_query for _, bundle in chunk)
        loop = asyncio.get_running_loop()
        monotonic = self._monotonic or loop.time
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                embedding = (
                    await self._embedding_factory()
                    if self._embedding_factory is not None else self._embedding
                )
                assert embedding is not None
                batch = await embedding.embed(
                    generation.profile,
                    texts,
                    monotonic() + self._attempt_timeout_seconds,
                    self._stop,
                )
            if batch.profile != generation.profile:
                raise EmbeddingContractError("embedding profile does not match generation")
            validated = validated_embedding_batch(
                generation.profile,
                tuple(vector.values for vector in batch.vectors),
                expected_count=len(chunk),
            )
        except EmbeddingCancelledError:
            if self._stop.is_requested():
                return
            await self._fail_chunk(chunk, "embedding_cancelled", retryable=False)
            return
        except (TimeoutError, PortTimeoutError, PortRateLimitError) as error:
            await self._fail_chunk(chunk, self._safe_code(error), retryable=True)
            return
        except PortUnavailableError as error:
            retryable = self._retryable_unavailable(error)
            await self._fail_chunk(chunk, self._safe_code(error), retryable=retryable)
            return
        except (
            EmbeddingContractError,
            InvalidPortResponseError,
            PortConfigurationError,
            PortResponseLimitError,
            ValueError,
        ) as error:
            await self._fail_chunk(chunk, self._safe_code(error), retryable=False)
            return

        if self._stop.is_requested():
            return

        for (job, bundle), vector in zip(chunk, validated.vectors, strict=True):
            if self._stop.is_requested():
                return
            try:
                await self._store.write_ready_projection(
                    job,
                    bundle.case.with_embedding(vector),
                    bundle.fragments,
                )
            except PortUnavailableError as error:
                await self._retry_or_fail(job, self._safe_code(error), retryable=True)
            except (ValueError, RuntimeError) as error:
                await self._retry_or_fail(job, self._safe_code(error), retryable=False)

    async def _fail_chunk(self, chunk, code: str, *, retryable: bool) -> None:
        for job, _ in chunk:
            await self._retry_or_fail(job, code, retryable=retryable)

    async def _retry_or_fail(
        self,
        job: RAGIndexJob,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        if retryable and job.attempts < 5:
            now = self._transition_time(job)
            delay = min(
                self._backoff_max_seconds,
                self._backoff_base_seconds * (2 ** max(job.attempts - 1, 0)),
            )
            await self._store.save(
                job.model_copy(
                    update={
                        "status": RAGIndexJobStatus.RETRY_WAIT,
                        "next_attempt_at": now + timedelta(seconds=delay),
                        "lease_owner": None,
                        "lease_until": None,
                        "last_safe_error_code": code,
                        "updated_at": now,
                    }
                )
            )
            return
        await self._terminal(job, RAGIndexJobStatus.FAILED, code)

    async def _terminal(
        self,
        job: RAGIndexJob,
        status: RAGIndexJobStatus,
        code: str,
    ) -> None:
        now = self._transition_time(job)
        await self._store.save(
            job.model_copy(
                update={
                    "status": status,
                    "next_attempt_at": None,
                    "lease_owner": None,
                    "lease_until": None,
                    "last_safe_error_code": code,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
        )

    def _transition_time(self, job: RAGIndexJob):
        return max(self._clock.now(), job.updated_at + timedelta(microseconds=1))

    @staticmethod
    def _safe_code(error: BaseException) -> str:
        if isinstance(error, PortRateLimitError):
            return "embedding_rate_limited"
        if isinstance(error, (TimeoutError, PortTimeoutError)):
            return "embedding_timeout"
        if isinstance(error, PortAuthenticationError):
            return "embedding_authentication_failed"
        if isinstance(error, PortUnavailableError):
            return "temporary_unavailable"
        if isinstance(error, (EmbeddingContractError, InvalidPortResponseError)):
            return "embedding_response_invalid"
        code = getattr(error, "code", None)
        if isinstance(code, str) and code.strip():
            return code
        if isinstance(error, RAGProjectionBuildError):
            return error.code
        if isinstance(error, RAGSourceReadError):
            return error.code
        return "indexing_protocol_invalid"

    @staticmethod
    def _retryable_unavailable(error: PortUnavailableError) -> bool:
        if isinstance(error, PortAuthenticationError):
            return False
        if isinstance(error, PortHttpError):
            return error.status_code == 408 or (
                error.status_code is not None and error.status_code >= 500
            )
        return True
