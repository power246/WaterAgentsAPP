import asyncio
from unittest.mock import patch

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
from app.ports import PortAuthenticationError, PortConfigurationError
from tests.application.guidelines.test_embedding import catalog
from tests.guideline_support import query
from tests.infrastructure.embeddings.test_openai_embeddings import http_client, response_body
from tests.infrastructure.test_m4_runtime import FixedClock


def credentials():
    return CredentialService(InMemoryKeyStore(), dict.fromkeys(CredentialName), FixedClock(), OfflineCredentialTester())


def test_attempt_lease_is_fixed_inflight_and_next_attempt_reads_new_key_without_persisting_secrets():
    async def exercise():
        service = credentials()
        await service.save(CredentialName.OPENAI, SecretStr("fixture-old-key"))
        selected = catalog(max_input_tokens=4096)
        started, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def handler(request):
            key = request.headers["Authorization"]
            seen.append(key)
            if key == "Bearer fixture-old-key":
                started.set()
                await release.wait()
            return httpx.Response(200, json=response_body(selected.embedding_profile.model, [[1, 0, 0]]))

        async with http_client(handler) as http:
            runtime = GuidelineRuntime(settings=AppSettings(_env_file=None), catalog=selected, credentials=service, http=http)
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
                with pytest.raises(PortAuthenticationError) as error:
                    await runtime.embedding_for_attempt()
                assert guideline_embedding_failure(error.value) == ("guideline_embedding_credentials_unavailable", False)
                assert lease.await_count == 3
            assert seen == ["Bearer fixture-old-key", "Bearer fixture-new-key", "Bearer fixture-old-key"]
            assert first._transport._http is second._transport._http is http
            assert first._timeout == second._timeout == 20
            assert not http._client.is_closed and "Authorization" not in http._client.headers
            rendered = profile.model_dump_json() + query(profile).model_dump_json() + repr(runtime) + repr(first)
            assert "fixture-old-key" not in rendered and "fixture-new-key" not in rendered and "provider.test" not in rendered
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
