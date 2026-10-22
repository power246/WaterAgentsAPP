import asyncio
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.application.guidelines.embedding import guideline_embedding_failure
from app.application.settings_admin.credential_service import CredentialService
from app.domain import ModelCatalogSnapshot
from app.domain.settings_admin import CredentialName
from app.domain.guidelines import GuidelinePolicy
from app.infrastructure.config import AppSettings
from app.infrastructure.credentials.memory_key_store import InMemoryKeyStore, OfflineCredentialTester
from app.infrastructure.runtime.guidelines import GuidelineRuntime
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.ports import PortAuthenticationError, PortConfigurationError
from tests.application.guidelines.test_embedding import catalog
from tests.guideline_support import query
from tests.infrastructure.embeddings.test_openai_embeddings import http_client, response_body
from tests.infrastructure.test_m4_runtime import FixedClock, catalog as runtime_catalog, settings as runtime_settings
from tests.m4_support import build_test_m8_runtime
from scripts.test_database import temporary_migrated_schema
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.infrastructure.config import load_parameter_capability_registry
from app.application.guidelines.authoring import GuidelineAuthoringService
from tests.application.guidelines.test_worker import Clock, custom, Embedding, worker
from tests.guideline_bundle_support import bundled_version, EmptyBundledSource


@pytest.fixture
def database_settings():
    with temporary_migrated_schema() as selected: yield selected


def credentials():
    return CredentialService(InMemoryKeyStore(), dict.fromkeys(CredentialName), FixedClock(), OfflineCredentialTester())


@pytest.mark.parametrize("independent", [False, True])
def test_attempt_lease_is_fixed_inflight_and_next_attempt_reads_new_key_without_persisting_secrets(independent):
    async def exercise():
        service = credentials()
        await service.save(CredentialName.OPENAI, SecretStr("fixture-old-key"))
        selected = catalog(max_input_tokens=4096)
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        expected_key = "fixture-independent-key" if independent else "fixture-old-key"

        async def handler(request):
            key = request.headers["Authorization"]
            seen.append(key)
            assert request.url.host == ("embedding.test" if independent else "api.openai.com")
            if key == f"Bearer {expected_key}" and len(seen) == 1:
                started.set()
                await release.wait()
            return httpx.Response(200, json=response_body(selected.embedding_profile.model, [[1, 0, 0]]))

        async with http_client(handler) as http:
            configured = AppSettings(_env_file=None, **({"embedding_base_url": "https://embedding.test/v1",
                "embedding_api_key": expected_key} if independent else {}))
            runtime = GuidelineRuntime(settings=configured, catalog=selected, credentials=service, http=http)
            profile = runtime.desired_profile
            with patch.object(service, "lease", wraps=service.lease) as lease:
                first = await runtime.embedding_for_attempt()
                task = asyncio.create_task(first.embed(profile, (query(profile),), None, None))
                await asyncio.wait_for(started.wait(), 1)
                await service.save(CredentialName.OPENAI, SecretStr("fixture-new-key"))
                second = await runtime.embedding_for_attempt()
                await second.embed(profile, (query(profile),), None, None)
                await service.delete(CredentialName.OPENAI)
                release.set()
                await task
                await first.embed(profile, (query(profile),), None, None)
                if independent:
                    await runtime.embedding_for_attempt()
                else:
                    with pytest.raises(PortAuthenticationError) as error:
                        await runtime.embedding_for_attempt()
                    assert guideline_embedding_failure(error.value) == ("guideline_embedding_credentials_unavailable", False)
                assert lease.await_count == (0 if independent else 3)
            assert seen == ([f"Bearer {expected_key}"] * 3 if independent else
                ["Bearer fixture-old-key", "Bearer fixture-new-key", "Bearer fixture-old-key"])
            assert first._transport._http is second._transport._http is http
            assert first._timeout == second._timeout == 20
            assert not http._client.is_closed and "Authorization" not in http._client.headers
            rendered = profile.model_dump_json() + query(profile).model_dump_json() + repr(runtime) + repr(first)
            assert "fixture-old-key" not in rendered and "fixture-new-key" not in rendered and "provider.test" not in rendered
    asyncio.run(exercise())


def test_application_starts_worker_and_cancels_inflight_http_before_closing_pool(database_settings, monkeypatch):
    import app.main as main
    monkeypatch.setattr('app.infrastructure.runtime.guidelines.PackageBundledGuidelineSource',lambda **kwargs:EmptyBundledSource())

    async def exercise():
        clock = Clock()
        registry = load_parameter_capability_registry()
        repository = PostgresGuidelineRepository(database_settings, clock=clock)
        authoring = GuidelineAuthoringService(repository, registry, clock)
        receipts = [await custom(authoring, f"Shutdown family {index}") for index in range(3)]
        capabilities = list(runtime_catalog())
        original = capabilities[-1].embedding_profile
        updated = type(original).model_validate({**original.model_dump(exclude={"profile_id", "profile_digest"}), "max_input_tokens": 8192})
        capabilities[-1] = capabilities[-1].model_copy(update={"embedding_profile": updated})
        configured = runtime_settings(model_catalog=tuple(capabilities), memory_enabled=False, shutdown_save_grace_seconds=0.01)
        entered = asyncio.Event()
        calls, cancelled = [], []
        pool = None

        async def handler(request):
            calls.append(request)
            if len(calls) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not pool.is_closed
                cancelled.append(request)

        class MockHttp(ProviderHttpClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(main, "ProviderHttpClient", MockHttp)
        application = main.create_app(configured, clock=clock, transcript_store=InMemoryTranscriptStore(),
            guideline_repository=repository, m8_runtime_builder=build_test_m8_runtime)
        try:
            async with application.router.lifespan_context(application):
                pool = application.state.provider_http._client
                runtime = application.state.guideline_runtime
                await asyncio.wait_for(entered.wait(), 4)
                assert len(calls) == 2 and not pool.is_closed
                assert runtime.worker.policy.worker_max_concurrency == 2
            assert runtime._task.done() and pool.is_closed and len(cancelled) == 2
            await runtime.shutdown()
            states = [(await authoring.get_operation(receipt.operation_id)).state.value for receipt in receipts]
            assert states.count("running") == 2 and states.count("pending") == 1
            assert all([(await authoring.get(receipt.document_id)).active_version_id is None for receipt in receipts])
            # A fresh worker after process loss reclaims durable leases, without another approval.
            clock.advance(61)
            recovered = worker(repository, Embedding(), runtime.desired_profile)
            for _ in range(3):
                await recovered.run_once()
            assert all([(await authoring.get_operation(receipt.operation_id)).state.value == "succeeded" for receipt in receipts])
            assert len(calls) == 2
        finally:
            await repository.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_guideline_runtime_and_profile_are_independent_of_memory_setting(memory_enabled):
    async def exercise():
        service = credentials()
        await service.save(CredentialName.OPENAI, SecretStr("fixture-secret"))
        selected = catalog(max_input_tokens=4096)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=response_body(selected.embedding_profile.model, [[1, 0, 0]]))

        async with http_client(handler) as http:
            runtime = GuidelineRuntime(settings=AppSettings(_env_file=None, memory_enabled=memory_enabled),
                catalog=selected, credentials=service, http=http)
            assert runtime.safe_error_code is None
            assert runtime.desired_profile.profile_id != selected.embedding_profile.profile_id
            run = runtime.embedding_for_lease(await service.lease(CredentialName.OPENAI))
            runtime.bind_run_embedding("fixture-run", run)
            with pytest.raises(ValueError): runtime.bind_run_embedding("fixture-run", run)
            await runtime.embedding_for_run("fixture-run").embed(runtime.desired_profile, (query(runtime.desired_profile),), None, None)
            runtime.release_run_embedding("fixture-run")
            runtime.release_run_embedding("fixture-run")
            assert runtime.embedding_for_run("fixture-run") is None
            assert len(calls) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["missing_profile", "unsupported_provider", "missing_http", "missing_key"])
def test_unavailable_configuration_never_dispatches_or_changes_analysis_configuration(bad):
    async def exercise():
        configured = AppSettings(_env_file=None, memory_enabled=False)
        original = configured.model_dump()
        selected = ModelCatalogSnapshot(capabilities=()) if bad == "missing_profile" else catalog(
            provider="unsupported" if bad == "unsupported_provider" else "openai", max_input_tokens=4096)
        service = credentials()
        calls = []

        def handler(request):
            calls.append(request)
            raise AssertionError("unavailable guideline embedding dispatched")

        async with http_client(handler) as http:
            runtime = GuidelineRuntime(settings=configured, catalog=selected, credentials=service,
                                      http=None if bad == "missing_http" else http)
            error_type = PortAuthenticationError if bad == "missing_key" else PortConfigurationError
            with pytest.raises(error_type): await runtime.embedding_for_attempt()
            if bad != "missing_key":
                assert runtime.desired_profile is None
                assert runtime.safe_error_code == "guideline_embedding_configuration_invalid"
            assert not calls and configured.model_dump() == original
    asyncio.run(exercise())


def test_first_missing_key_then_restart_and_manual_retry_activates_same_approved_version(database_settings):
    async def exercise():
        registry = load_parameter_capability_registry()
        clock = Clock()
        repository = PostgresGuidelineRepository(database_settings, clock=clock)
        selected = catalog(max_input_tokens=8192)
        service = credentials()
        approved = bundled_version()
        calls = []

        class Source:
            def read_versions(self): return (approved,)

        def handler(request):
            calls.append(request.headers["Authorization"])
            return httpx.Response(200, json=response_body(selected.embedding_profile.model, [[1, 0, 0]]))

        async def wait_for_state(state):
            async with asyncio.timeout(4):
                while True:
                    operation = await repository.read_document_details(approved.identity.guideline_id)
                    if state == "succeeded" and operation.active_version_id == approved.version_id:
                        return operation
                    if operation.preparation is not None and operation.preparation.state.value == state:
                        return operation
                    await asyncio.sleep(0.01)

        async with http_client(handler) as http:
            runtime = GuidelineRuntime(settings=AppSettings(_env_file=None, memory_enabled=False), catalog=selected, credentials=service, http=http)
            try:
                await runtime.start(repository, registry, source=Source())
                failed = await wait_for_state("failed")
                assert failed.active_version_id is None and (await repository.read_catalog()).generation_id is not None
                assert not calls
                await runtime.shutdown()
                await runtime.shutdown()
                await service.save(CredentialName.OPENAI, SecretStr("fixture-recovered-key"))
                restarted = GuidelineRuntime(settings=AppSettings(_env_file=None, memory_enabled=False), catalog=selected, credentials=service, http=http)
                await restarted.start(repository, registry, source=Source())
                try:
                    authoring = GuidelineAuthoringService(repository, registry, clock)
                    retry = await authoring.retry(failed.preparation.receipt.operation_id, failed.revision, uuid4())
                    result = await wait_for_state("succeeded")
                    assert retry.version_id == result.active_version_id == approved.version_id
                    assert (await authoring.get_operation(retry.operation_id)).state.value == "succeeded"
                    assert len(calls) == 1 and calls[0] == "Bearer fixture-recovered-key"
                    with pytest.raises(RuntimeError): await restarted.start(repository, registry)
                finally: await restarted.shutdown()
            finally:
                await runtime.shutdown()
                await repository.aclose()
    asyncio.run(exercise())


def test_run_pin_retrieval_uses_fixed_run_lease_once_and_empty_library_dispatches_zero(database_settings):
    from datetime import timedelta
    from app.application.analysis_runs.workflow import CancellationToken
    from tests.application.guidelines.test_retrieval import task

    async def exercise():
        clock=Clock()
        registry=load_parameter_capability_registry()
        repository=PostgresGuidelineRepository(database_settings,clock=clock)
        service=credentials()
        await service.save(CredentialName.OPENAI,SecretStr("fixture-pinned-run-key"))
        calls=[]
        selected=catalog(max_input_tokens=8192)
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json=response_body(selected.embedding_profile.model,[[1,0,0]]))
        async with http_client(handler) as http:
            runtime=GuidelineRuntime(settings=AppSettings(_env_file=None,memory_enabled=False),catalog=selected,
                credentials=service,http=http,clock=clock)
            try:
                await runtime.start(repository,registry,source=EmptyBundledSource())
                # Stop preparation polling; use the same durable preparation implementation deterministically.
                await runtime.shutdown()
                preparation=worker(repository,Embedding(),runtime.desired_profile)
                await preparation.run_once()
                run_id=uuid4()
                runtime.bind_run_embedding(str(run_id),runtime.embedding_for_lease(await service.lease(CredentialName.OPENAI)))
                token=CancellationToken(asyncio.Event())
                empty_run=await runtime.pin_run(run_id,token)
                empty=empty_run.session(task(run_id,token),deadline=clock.now()+timedelta(seconds=60))
                try: assert (await empty.get_or_load()).status=="empty_library" and not calls
                finally: await empty.aclose()
                authoring=GuidelineAuthoringService(repository,registry,clock)
                receipt=await custom(authoring)
                await preparation.run_once()
                runtime.release_run_embedding(str(run_id))
                run_id=uuid4()
                runtime.bind_run_embedding(str(run_id),runtime.embedding_for_lease(await service.lease(CredentialName.OPENAI)))
                ready_run=await runtime.pin_run(run_id,token)
                assert ready_run.pin.catalog!=empty_run.pin.catalog
                await service.save(CredentialName.OPENAI,SecretStr("fixture-replacement-key"))
                session=ready_run.session(task(run_id,token),deadline=clock.now()+timedelta(seconds=60))
                try:
                    first,second=await asyncio.gather(session.get_or_load(),session.get_or_load())
                    assert first is second and first.matches[0].version.version_id==receipt.version_id
                    assert len(calls)==1 and calls[0].headers["Authorization"]=="Bearer fixture-pinned-run-key"
                finally: await session.aclose()
                runtime.release_run_embedding(str(run_id))
                assert runtime.embedding_for_run(str(run_id)) is None
            finally:
                await runtime.shutdown()
                await repository.aclose()
    asyncio.run(exercise())
