import asyncio

import httpx
from pydantic import SecretStr

from app.application.settings_admin.credential_service import CredentialService
from app.domain import RAGIndexJobStatus
from app.domain.settings_admin import CredentialName
from app.infrastructure.config import AppSettings
from app.infrastructure.credentials.memory_key_store import InMemoryKeyStore, OfflineCredentialTester
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.observability.rag_metrics import RAGRuntimeMetrics
from app.infrastructure.runtime.m8 import AttemptEmbeddingFactory
from tests.application.transcript_memory.test_worker import (
    _Clock, _Loader, _Store, _generation, _job, _worker,
)
from tests.infrastructure.embeddings.support import embedding_profile


def _credentials():
    store = InMemoryKeyStore()
    return store, CredentialService(
        store, {CredentialName.OPENAI: None, CredentialName.GOOGLE_GEOCODING: None},
        _Clock(), OfflineCredentialTester(),
    )


def _response(profile):
    return {"object": "list", "model": profile.model, "data": [
        {"object": "embedding", "index": 0,
         "embedding": [0.125] * profile.output_dimensions},
    ]}


def test_attempt_lease_is_fixed_until_batch_ends_and_next_attempt_uses_replacement():
    async def exercise():
        _, credentials = _credentials()
        await credentials.save(CredentialName.OPENAI, SecretStr("old-attempt-private-0001"))
        profile = embedding_profile()
        generation = _generation().model_copy(update={"profile": profile})
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def handler(request):
            auth = request.headers["Authorization"]
            seen.append(auth)
            if auth == "Bearer old-attempt-private-0001":
                entered.set()
                await release.wait()
            return httpx.Response(200, json=_response(profile))

        async with ProviderHttpClient(
            timeout_seconds=20, max_response_bytes=4096,
            retry_delay_seconds=0, attempts=1,
            transport=httpx.MockTransport(handler),
        ) as http:
            factory = AttemptEmbeddingFactory(
                credentials=credentials, http=http,
                settings=AppSettings(_env_file=None),
                metrics=RAGRuntimeMetrics(), monotonic=asyncio.get_running_loop().time,
            )
            old_store = _Store((_job(),))
            old_worker = _worker(old_store, _Loader(), None, embedding_factory=factory)
            old_task = asyncio.create_task(old_worker.run_once(generation))
            await asyncio.wait_for(entered.wait(), 2)
            await credentials.save(CredentialName.OPENAI, SecretStr("new-attempt-private-0002"))
            new_store = _Store((_job(attempts=1, suffix=2),))
            new_worker = _worker(new_store, _Loader(), None, embedding_factory=factory)
            assert await new_worker.run_once(generation) == 1
            release.set()
            assert await old_task == 1
            assert len(old_store.projections) == len(new_store.projections) == 1
            assert seen == [
                "Bearer old-attempt-private-0001",
                "Bearer new-attempt-private-0002",
            ]

    asyncio.run(exercise())


def test_missing_or_unavailable_credential_fails_only_index_job():
    async def exercise():
        store, credentials = _credentials()
        profile = embedding_profile()
        generation = _generation().model_copy(update={"profile": profile})

        async def handler(request):
            raise AssertionError("missing credential must not call provider")

        async with ProviderHttpClient(
            timeout_seconds=20, max_response_bytes=4096,
            retry_delay_seconds=0, attempts=1,
            transport=httpx.MockTransport(handler),
        ) as http:
            factory = AttemptEmbeddingFactory(
                credentials=credentials, http=http,
                settings=AppSettings(_env_file=None),
                metrics=RAGRuntimeMetrics(), monotonic=asyncio.get_running_loop().time,
            )
            for unavailable in (False, True):
                store.available = not unavailable
                jobs = _Store((_job(),))
                assert await _worker(jobs, _Loader(), None, embedding_factory=factory).run_once(generation) == 1
                assert jobs.projections == []
                assert jobs.saved[-1].status is RAGIndexJobStatus.FAILED
                assert jobs.saved[-1].last_safe_error_code == "embedding_authentication_failed"

    asyncio.run(exercise())


def test_attempt_transport_retry_then_new_lease_and_shutdown_cancellation():
    async def exercise():
        _, credentials = _credentials()
        await credentials.save(CredentialName.OPENAI, SecretStr("retry-attempt-private-0001"))
        profile = embedding_profile()
        generation = _generation().model_copy(update={"profile": profile})
        seen = []
        blocked, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            auth = request.headers["Authorization"]
            seen.append(auth)
            if auth == "Bearer retry-attempt-private-0001":
                return httpx.Response(503, json={"error": "unavailable"})
            blocked.set()
            await release.wait()
            return httpx.Response(200, json=_response(profile))

        async with ProviderHttpClient(
            timeout_seconds=20, max_response_bytes=4096,
            retry_delay_seconds=0, attempts=1,
            transport=httpx.MockTransport(handler),
        ) as http:
            factory = AttemptEmbeddingFactory(
                credentials=credentials, http=http,
                settings=AppSettings(_env_file=None),
                metrics=RAGRuntimeMetrics(), monotonic=asyncio.get_running_loop().time,
            )
            retry_store = _Store((_job(),))
            assert await _worker(retry_store, _Loader(), None, embedding_factory=factory).run_once(generation) == 1
            assert retry_store.saved[-1].status is RAGIndexJobStatus.RETRY_WAIT
            await credentials.save(CredentialName.OPENAI, SecretStr("shutdown-attempt-private-0002"))
            stopped_store = _Store((_job(attempts=1, suffix=3),))
            worker = _worker(stopped_store, _Loader(), None, embedding_factory=factory)
            task = asyncio.create_task(worker.run_once(generation))
            await asyncio.wait_for(blocked.wait(), 2)
            worker.request_stop()
            assert await asyncio.wait_for(task, 2) == 1
            assert stopped_store.projections == []
            assert stopped_store.saved == []
            release.set()
            assert seen == [
                "Bearer retry-attempt-private-0001",
                "Bearer shutdown-attempt-private-0002",
            ]

    asyncio.run(exercise())
