import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.application.settings_admin.credential_service import CredentialService
from app.application.settings_admin.errors import CredentialMissingError
from app.application.transcripts.models import AnalysisRequestV1
from app.domain import ModelSelection
from app.domain.guidelines import GuidelinePolicy
from app.domain.settings_admin import CredentialName
from app.infrastructure.config import ModelCatalog, load_parameter_capability_registry
from app.infrastructure.credentials.memory_key_store import (
    InMemoryKeyStore,
    OfflineCredentialTester,
)
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime.provider_bundle import RunProviderBundleFactory
from app.infrastructure.runtime.guidelines import GuidelineRuntime
from app.main import create_app
from tests.infrastructure.embeddings.support import canonical_text
from tests.infrastructure.test_m4_runtime import FixedClock, settings
from tests.m4_support import FakeM8Runtime
from tests.guideline_support import query as guideline_query

OPENAI_OLD = "old-openai-private-0001"
OPENAI_NEW = "new-openai-private-0002"
GOOGLE_OLD = "old-google-private-0003"
GOOGLE_NEW = "new-google-private-0004"


class _M8Bindings:
    def __init__(self):
        self.ports = {}

    def bind_run_embedding(self, run_id, port):
        assert run_id not in self.ports
        self.ports[run_id] = port

    def release_run_embedding(self, run_id):
        self.ports.pop(run_id)


@pytest.mark.parametrize("independent", [False, True])
def test_run_bundle_leases_survive_replace_and_delete_with_one_shared_pool(independent):
    async def exercise():
        configured = settings(openai_api_key=None, google_geocoding_api_key=None, memory_enabled=False,
            guideline_policy=GuidelinePolicy(document_instructions="passage: ", query_instructions="query: "),
            **({"embedding_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "embedding_api_key": "fixture-independent-embedding-key"} if independent else {}))
        first_embedding_key = "fixture-independent-embedding-key" if independent else OPENAI_OLD
        store = InMemoryKeyStore()
        credentials = CredentialService(
            store, {CredentialName.OPENAI: None, CredentialName.GOOGLE_GEOCODING: None},
            FixedClock(), OfflineCredentialTester(),
        )
        with pytest.raises(CredentialMissingError):
            await credentials.lease(CredentialName.OPENAI)
        await credentials.save(CredentialName.OPENAI, SecretStr(OPENAI_OLD))
        await credentials.save(CredentialName.GOOGLE_GEOCODING, SecretStr(GOOGLE_OLD))
        profile = configured.model_catalog[2].embedding_profile
        assert profile is not None
        request = AnalysisRequestV1(
            query="hydrology", parser_model=ModelSelection(provider="openai", model="parser-v1"),
            waterbody_model=ModelSelection(provider="openai", model="waterbody-v1"),
            memory_mode="stateless",
        )
        started, release = asyncio.Event(), asyncio.Event()
        guideline_started = asyncio.Event()
        seen = []

        async def handler(request):
            seen.append(request.headers.get("Authorization"))
            assert request.url.host == ("generativelanguage.googleapis.com" if independent else "api.openai.com")
            assert request.url.path == ("/v1beta/openai/embeddings" if independent else "/v1/embeddings")
            if request.headers["Authorization"] == f"Bearer {first_embedding_key}":
                started.set()
                if json.loads(request.content)["input"][0].startswith("query: "):
                    guideline_started.set()
                if len(seen) <= 2:
                    await release.wait()
            return httpx.Response(200, json={
                "object": "list", "model": profile.model,
                "data": [{"object": "embedding", "index": 0,
                          "embedding": [0.125] * profile.output_dimensions}],
            })

        m8 = _M8Bindings()
        async with ProviderHttpClient(
            timeout_seconds=20, max_response_bytes=4096,
            retry_delay_seconds=0, attempts=1,
            transport=httpx.MockTransport(handler),
        ) as http:
            assert "Authorization" not in http._client.headers
            guidelines = GuidelineRuntime(
                settings=configured, catalog=ModelCatalog(configured.model_catalog).snapshot(),
                credentials=credentials, http=http,
            )
            factory = RunProviderBundleFactory(
                settings=configured, credentials=credentials, http=http,
                operations=object(), m5_runtime=SimpleNamespace(
                    acquisition=object(), transcript=object(),
                ), m8_runtime=m8, upstream_results=object(),
                guideline_runtime=guidelines,
                result_reader=object(),
                capability_registry=load_parameter_capability_registry(),
                model_catalog=ModelCatalog(configured.model_catalog).snapshot(),
                clock=FixedClock(), id_factory=uuid4,
            )
            first = await factory.build(request.to_configuration_snapshot("America/Chicago"))
            first_id = uuid4()
            first.bind_run(first_id)
            first_workflow = first.workflow
            assert first_workflow.execution_bound
            first_parser = first_workflow._query_understanding._parser
            first_location = first_workflow._query_understanding._context_resolver._location_resolver
            first_google = first_location._geocoding
            first_nldi = first_location._comid_lookup
            assert first_nldi._request_state is factory._nldi_request_state
            assert first_parser._api_key == OPENAI_OLD
            assert first_google._api_key == GOOGLE_OLD
            first_stage = first_workflow._execution_stage
            first_model = first_stage._summary_boundary._m6_runtime.model_port_router.require(
                request.waterbody_model,
            ).port
            assert first_model._api_key == OPENAI_OLD
            assert first_model._http is first_parser._http is first_google._http is http
            pending = asyncio.create_task(m8.ports[str(first_id)].embed(
                profile, (canonical_text(),), None, None,
            ))
            await asyncio.wait_for(started.wait(), timeout=2)
            guideline_profile = guidelines.desired_profile
            pending_guideline = asyncio.create_task(guidelines.embedding_for_run(str(first_id)).embed(
                guideline_profile, (guideline_query(guideline_profile),), None, None,
            ))
            await asyncio.wait_for(guideline_started.wait(), timeout=2)
            await credentials.save(CredentialName.OPENAI, SecretStr(OPENAI_NEW))
            await credentials.save(CredentialName.GOOGLE_GEOCODING, SecretStr(GOOGLE_NEW))
            second = await factory.build(request.to_configuration_snapshot("America/Chicago"))
            second_id = uuid4()
            second.bind_run(second_id)
            assert second.workflow._query_understanding._parser._api_key == OPENAI_NEW
            second_location = second.workflow._query_understanding._context_resolver._location_resolver
            assert second_location._geocoding._api_key == GOOGLE_NEW
            second_nldi = second_location._comid_lookup
            assert second_nldi is not first_nldi
            assert second_nldi._request_state is first_nldi._request_state
            await m8.ports[str(second_id)].embed(profile, (canonical_text(),), None, None)
            await guidelines.embedding_for_run(str(second_id)).embed(
                guideline_profile, (guideline_query(guideline_profile),), None, None,
            )
            await credentials.delete(CredentialName.OPENAI)
            await credentials.delete(CredentialName.GOOGLE_GEOCODING)
            with pytest.raises(CredentialMissingError):
                await factory.build(request.to_configuration_snapshot("America/Chicago"))
            release.set()
            await pending
            await pending_guideline
            await m8.ports[str(first_id)].embed(profile, (canonical_text(),), None, None)
            await guidelines.embedding_for_run(str(first_id)).embed(
                guideline_profile, (guideline_query(guideline_profile),), None, None,
            )
            expected = ([first_embedding_key] * 6 if independent else
                [OPENAI_OLD, OPENAI_OLD, OPENAI_NEW, OPENAI_NEW, OPENAI_OLD, OPENAI_OLD])
            assert seen == [f"Bearer {key}" for key in expected]
            if independent:
                assert first_embedding_key in first_stage._summary_boundary._m6_runtime.private_values
                assert first_embedding_key in first_workflow._query_understanding._private_values
            await first.aclose()
            await first.aclose()
            await second.aclose()
            assert m8.ports == {}
            assert guidelines.embedding_for_run(str(first_id)) is guidelines.embedding_for_run(str(second_id)) is None
            assert not http._client.is_closed
            assert OPENAI_OLD not in repr(first)
            assert GOOGLE_NEW not in repr(second)

    asyncio.run(exercise())


def test_production_composition_opens_one_secret_free_pool_and_one_worker(monkeypatch):
    async def exercise():
        from app import main as main_module

        opened = []
        original_http = main_module.ProviderHttpClient

        class TrackedProviderHttp(original_http):
            async def __aenter__(self):
                opened.append(self)
                return await super().__aenter__()

        monkeypatch.setattr(main_module, "ProviderHttpClient", TrackedProviderHttp)

        class FakeM8(FakeM8Runtime, _M8Bindings):
            def __init__(self):
                FakeM8Runtime.__init__(self)
                _M8Bindings.__init__(self)

        m8 = FakeM8()

        @asynccontextmanager
        async def m8_builder(*args, **kwargs):
            yield m8

        configured = settings(openai_api_key=None, google_geocoding_api_key=None)
        app = create_app(
            configured, transcript_store=InMemoryTranscriptStore(),
            m8_runtime_builder=m8_builder, key_store=InMemoryKeyStore(),
            credential_tester=OfflineCredentialTester(),
        )
        async with app.router.lifespan_context(app):
            assert len(opened) == 1
            assert app.state.provider_http is opened[0]
            assert "Authorization" not in opened[0]._client.headers
            assert isinstance(app.state.provider_bundle_factory, RunProviderBundleFactory)
            assert app.state.provider_bundle_factory._http is opened[0]
            assert app.state.guideline_runtime.desired_profile is not None
            assert app.state.guideline_runtime._http is opened[0]
            assert app.state.m6_runtime is None
            assert m8.started and not m8.stopped
            credentials = app.state.credential_service
            await credentials.save(CredentialName.OPENAI, SecretStr(OPENAI_NEW))
            await credentials.save(CredentialName.GOOGLE_GEOCODING, SecretStr(GOOGLE_NEW))
            request = AnalysisRequestV1(
                query="hydrology",
                parser_model=ModelSelection(provider="openai", model="parser-v1"),
                waterbody_model=ModelSelection(provider="openai", model="waterbody-v1"),
                memory_mode="stateless",
            )
            bundle = await app.state.provider_bundle_factory.build(
                request.to_configuration_snapshot("America/Chicago"),
            )
            run_id = uuid4()
            bundle.bind_run(run_id)
            assert str(run_id) in m8.ports
            assert app.state.guideline_runtime.embedding_for_run(str(run_id)) is not None
            await bundle.aclose()
            assert m8.ports == {}
            assert app.state.guideline_runtime.embedding_for_run(str(run_id)) is None
            assert len(opened) == 1
        assert m8.stopped
        assert opened[0]._client is None

    asyncio.run(exercise())
