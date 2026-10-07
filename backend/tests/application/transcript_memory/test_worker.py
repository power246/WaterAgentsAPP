import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.application.transcript_memory import (
    RAGIndexWorker,
    RAGSourceReadError,
    RAGSourceUnavailableError,
    TranscriptRAGCaseSourceReader,
)
from app.application.transcripts.read_models import RegisteredRunRead, StoredRunHeader
from app.domain import (
    EmbeddingBatch,
    EmbeddingProfile,
    EmbeddingVector,
    HistoricalCaseRef,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexJob,
    RAGIndexJobStatus,
)
from app.ports import InvalidPortResponseError, PortTimeoutError
from tests.application.transcript_memory.m3_support import REGISTRY, case_source
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort


NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
GENERATION_ID = "86000000-0000-4000-8000-000000000001"


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class _Store:
    def __init__(self, jobs: tuple[RAGIndexJob, ...]) -> None:
        self.jobs = list(jobs)
        self.saved: list[RAGIndexJob] = []
        self.projections = []
        self.claimed = False

    async def claim(self, generation_id, lease_owner, lease_until, limit):
        if self.claimed:
            return ()
        self.claimed = True
        claimed = []
        for job in self.jobs[:limit]:
            claimed.append(
                job.model_copy(
                    update={
                        "status": RAGIndexJobStatus.LEASED,
                        "attempts": job.attempts + 1,
                        "lease_owner": lease_owner,
                        "lease_until": lease_until,
                        "updated_at": NOW,
                    }
                )
            )
        return tuple(claimed)

    async def save(self, job):
        self.saved.append(job)

    async def write_ready_projection(self, job, case, fragments):
        self.projections.append((job, case, fragments))


class _Loader:
    def __init__(self, source=None, error: Exception | None = None) -> None:
        self.source = source or case_source()
        self.error = error

    async def load_case_source(self, job):
        if self.error is not None:
            raise self.error
        return self.source


class _ErrorEmbedding:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def embed(self, profile, texts, deadline, cancel_token):
        raise self.error


class _InvalidBatchEmbedding:
    async def embed(self, profile, texts, deadline, cancel_token):
        vector = EmbeddingVector.model_construct(values=(float("nan"),) * 3)
        return EmbeddingBatch.model_construct(
            profile=profile,
            vectors=tuple(vector for _ in texts),
        )


class _BlockedEmbedding:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, profile, texts, deadline, cancel_token):
        self.started.set()
        await self.release.wait()
        return await DeterministicEmbeddingPort().embed(
            profile, texts, deadline, None
        )


class _RegisteredRunReader:
    def __init__(self, snapshot: RegisteredRunRead) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def read_registered_run(self, run_id, view):
        self.calls += 1
        return self.snapshot


def _generation() -> RAGIndexGeneration:
    profile = EmbeddingProfile(
        provider="test-provider",
        model="embedding-3",
        output_dimensions=3,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )
    return RAGIndexGeneration(
        generation_id=GENERATION_ID,
        profile=profile,
        status=RAGIndexGenerationStatus.BUILDING,
        source_cutoff_created_at=NOW,
        source_cutoff_item_id=str(UUID(int=1)),
        created_at=NOW,
    )


def _job(*, attempts: int = 0, suffix: int = 1) -> RAGIndexJob:
    source = case_source()
    return RAGIndexJob(
        job_id=f"86000000-0000-4000-8000-{suffix:012d}",
        generation_id=GENERATION_ID,
        source_run_id=str(source.result.item.run_id),
        source_session_id=str(source.result.item.session_id),
        source_result_item_id=str(source.result.item.item_id),
        status=RAGIndexJobStatus.PENDING,
        attempts=attempts,
        created_at=NOW - timedelta(seconds=1),
        updated_at=NOW - timedelta(seconds=1),
    )


def _worker(store, loader, embedding):
    return RAGIndexWorker(
        store=store,
        source_loader=loader,
        embedding=embedding,
        clock=_Clock(),
        lease_owner="worker-a",
        claim_batch_size=4,
        embedding_batch_size=4,
        max_concurrency=1,
        lease_seconds=60,
        attempt_timeout_seconds=30,
        backoff_base_seconds=2,
        backoff_max_seconds=5,
    )


def test_worker_builds_and_writes_one_atomic_ready_projection() -> None:
    async def exercise() -> None:
        store = _Store((_job(),))
        worker = _worker(store, _Loader(), DeterministicEmbeddingPort())

        assert await worker.run_once(_generation()) == 1
        assert len(store.projections) == 1
        assert store.saved == []
        _, case, fragments = store.projections[0]
        assert case.profile_id == _generation().profile.profile_id
        assert len(case.embedding.values) == 3
        assert fragments

    asyncio.run(exercise())


def test_transcript_source_loader_decodes_authoritative_run_once() -> None:
    async def exercise() -> None:
        source = case_source()
        typed = (
            source.analysis_context,
            source.topology,
            *source.fragments,
            source.result,
        )
        snapshot = RegisteredRunRead(
            run=StoredRunHeader(
                run_id=source.result.item.run_id,
                status=source.run_status,
                created_at=NOW - timedelta(minutes=1),
                updated_at=NOW,
                query="persisted source fixture",
                request_item_id=UUID(int=101),
                created_item_id=UUID(int=102),
                terminal_item_id=UUID(int=103),
            ),
            sessions=(source.session,),
            items=tuple(
                REGISTRY.parse(item.item, item.item.inline_body or "") for item in typed
            ),
        )
        reader = _RegisteredRunReader(snapshot)
        loader = TranscriptRAGCaseSourceReader(reader, REGISTRY)
        loaded = await loader.load_case_source(_job())
        hydrated = await loader.load_historical_case_source(
            HistoricalCaseRef(
                generation_id=GENERATION_ID,
                case_id="86000000-0000-4000-8000-000000000099",
                run_id=str(source.result.item.run_id),
                session_id=str(source.result.item.session_id),
                target_comid=source.result.body.target_comid,
                analysis_context_item_id=str(source.analysis_context.item.item_id),
                result_item_id=str(source.result.item.item_id),
            )
        )

        assert reader.calls == 2
        assert loaded.result.item.item_id == source.result.item.item_id
        assert hydrated.result == loaded.result
        assert loaded.session == source.session
        assert loaded.confirmed_references == source.confirmed_references[:1]

    asyncio.run(exercise())


def test_retry_backoff_is_bounded_and_fifth_attempt_fails_immediately() -> None:
    async def exercise() -> None:
        timeout = PortTimeoutError("timeout", provider="test", operation="embed")
        retry_store = _Store((_job(attempts=2),))
        await _worker(retry_store, _Loader(), _ErrorEmbedding(timeout)).run_once(
            _generation()
        )
        retry = retry_store.saved[-1]
        assert retry.status is RAGIndexJobStatus.RETRY_WAIT
        assert retry.attempts == 3
        assert retry.next_attempt_at == retry.updated_at + timedelta(seconds=5)

        exhausted_store = _Store((_job(attempts=4),))
        await _worker(exhausted_store, _Loader(), _ErrorEmbedding(timeout)).run_once(
            _generation()
        )
        exhausted = exhausted_store.saved[-1]
        assert exhausted.status is RAGIndexJobStatus.FAILED
        assert exhausted.attempts == 5
        assert exhausted.next_attempt_at is None

    asyncio.run(exercise())


def test_invalid_embedding_batch_and_source_schema_never_write_partial_projection() -> None:
    async def exercise() -> None:
        invalid_store = _Store((_job(suffix=1), _job(suffix=2)))
        await _worker(invalid_store, _Loader(), _InvalidBatchEmbedding()).run_once(
            _generation()
        )
        assert invalid_store.projections == []
        assert {job.status for job in invalid_store.saved} == {
            RAGIndexJobStatus.FAILED
        }

        schema_store = _Store((_job(),))
        loader = _Loader(error=RAGSourceReadError("historical_source_schema_unsupported"))
        await _worker(schema_store, loader, DeterministicEmbeddingPort()).run_once(
            _generation()
        )
        assert schema_store.projections == []
        assert schema_store.saved[-1].status is RAGIndexJobStatus.FAILED

    asyncio.run(exercise())


def test_temporary_source_unavailability_uses_bounded_retry() -> None:
    async def exercise() -> None:
        store = _Store((_job(),))
        loader = _Loader(error=RAGSourceUnavailableError("database unavailable"))
        await _worker(store, loader, DeterministicEmbeddingPort()).run_once(
            _generation()
        )

        assert store.projections == []
        assert store.saved[-1].status is RAGIndexJobStatus.RETRY_WAIT
        assert store.saved[-1].last_safe_error_code == "source_temporarily_unavailable"

    asyncio.run(exercise())


def test_source_that_loses_candidate_eligibility_is_excluded() -> None:
    async def exercise() -> None:
        source = case_source()
        ineligible_result = replace(
            source.result,
            item=source.result.item.model_copy(update={"rag_eligible": False}),
        )
        store = _Store((_job(),))
        await _worker(
            store,
            _Loader(replace(source, result=ineligible_result)),
            DeterministicEmbeddingPort(),
        ).run_once(_generation())

        assert store.projections == []
        assert store.saved[-1].status is RAGIndexJobStatus.EXCLUDED
        assert store.saved[-1].last_safe_error_code == "source_no_longer_eligible"

    asyncio.run(exercise())


def test_shutdown_timeout_leaves_inflight_lease_recoverable() -> None:
    async def exercise() -> None:
        embedding = _BlockedEmbedding()
        store = _Store((_job(),))
        worker = _worker(store, _Loader(), embedding)
        running = asyncio.create_task(worker.run_once(_generation()))
        await embedding.started.wait()

        assert not await worker.drain(0.001)
        assert store.saved == []
        assert store.projections == []

        embedding.release.set()
        await running
        assert store.saved == []
        assert store.projections == []

    asyncio.run(exercise())
