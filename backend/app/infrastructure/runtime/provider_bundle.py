"""A run owns its provider adapters and immutable credential leases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from uuid import UUID

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from app.application.analysis_runs.workflow import AnalysisWorkflow
from app.application.evidence_tools import (
    RunLocalUpstreamResultRegistry,
    UpstreamInferenceResultReader,
)
from app.application.settings_admin.credential_service import CredentialService
from app.domain import ModelCatalogSnapshot, ModelRole, ParameterCapabilityRegistry
from app.domain.settings_admin import CredentialName
from app.infrastructure.adapters import OpenAIEmbeddingAdapter
from app.infrastructure.adapters.nldi_request_state import NldiRequestState
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.runtime.m4 import build_m4_runtime
from app.infrastructure.runtime.m5 import M5Runtime
from app.infrastructure.runtime.m6 import build_m6_runtime
from app.infrastructure.runtime.m7 import compose_m7_execution_stage
from app.infrastructure.runtime.m8 import M8Runtime
from app.ports.clock import ClockPort
from app.ports.settings_store import SettingsStorePort


class RunProviderBundle:
    def __init__(self, workflow: AnalysisWorkflow, stack: AsyncExitStack,
                 embedding: OpenAIEmbeddingAdapter, m8_runtime: M8Runtime) -> None:
        self.workflow = workflow
        self._stack = stack
        self._embedding = embedding
        self._m8_runtime = m8_runtime
        self._run_id: UUID | None = None
        self._closed = False

    def bind_run(self, run_id: UUID) -> None:
        if self._closed or self._run_id is not None:
            raise RuntimeError("provider bundle cannot be rebound")
        self._m8_runtime.bind_run_embedding(str(run_id), self._embedding)
        self._run_id = run_id

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._run_id is not None:
                self._m8_runtime.release_run_embedding(str(self._run_id))
        finally:
            await self._stack.aclose()


class RunProviderBundleFactory:
    def __init__(self, *, settings: AppSettings, credentials: CredentialService,
                 http: ProviderHttpClient, operations: object, m5_runtime: M5Runtime,
                 m8_runtime: M8Runtime, upstream_results: RunLocalUpstreamResultRegistry,
                 result_reader: UpstreamInferenceResultReader,
                 capability_registry: ParameterCapabilityRegistry,
                 model_catalog: ModelCatalogSnapshot, clock: ClockPort,
                 id_factory: Callable[[], UUID],
                 unit_policy_store: SettingsStorePort | None = None) -> None:
        self._settings = settings
        self._credentials = credentials
        self._http = http
        self._operations = operations
        self._m5_runtime = m5_runtime
        self._m8_runtime = m8_runtime
        self._upstream_results = upstream_results
        self._result_reader = result_reader
        self._capabilities = capability_registry
        self._catalog = model_catalog
        self._clock = clock
        self._id_factory = id_factory
        self._unit_policy_store = unit_policy_store
        self._nldi_request_state = NldiRequestState(
            cache_ttl_seconds=3600.0,
            cache_max_entries=512,
        )

    async def build(self, configuration: AnalysisConfigurationSnapshot) -> RunProviderBundle:
        if not self._catalog.supports(configuration.parser_model, ModelRole.QUERY_PARSER):
            raise SettingsConfigurationError("parser model is not configured")
        if not self._catalog.supports(configuration.waterbody_model, ModelRole.WATERBODY):
            raise SettingsConfigurationError("waterbody model is not configured")
        # Both reads happen once, before the creation owner can publish a run.
        openai = await self._credentials.lease(CredentialName.OPENAI)
        google = await self._credentials.lease(CredentialName.GOOGLE_GEOCODING)
        settings = self._settings.model_copy(update={
            "openai_api_key": openai.value,
            "google_geocoding_api_key": google.value,
        })
        stack = AsyncExitStack()
        try:
            m4 = await stack.enter_async_context(build_m4_runtime(
                settings, clock=self._clock,
                parameter_capabilities=self._capabilities,
                error_id_factory=self._id_factory, provider_http=self._http,
                unit_policy_store=self._unit_policy_store,
                nldi_request_state=self._nldi_request_state,
            ))
            m6 = await stack.enter_async_context(build_m6_runtime(
                settings, operations=self._operations, m5_runtime=self._m5_runtime,
                capability_registry=self._capabilities, model_catalog=self._catalog,
                clock=self._clock, operation_id_factory=self._id_factory,
                result_id_factory=lambda: str(self._id_factory()),
                error_id_factory=self._id_factory, provider_http=self._http,
            ))
            workflow = m4.query_topology_workflow
            assert workflow is not None
            workflow.bind_execution_stage(compose_m7_execution_stage(
                settings, operations=self._operations, m5_runtime=self._m5_runtime,
                m6_runtime=m6, upstream_results=self._upstream_results,
                result_reader=self._result_reader,
                capability_registry=self._capabilities, model_catalog=self._catalog,
                clock=self._clock, result_id_factory=lambda: str(self._id_factory()),
                error_id_factory=self._id_factory, operation_id_factory=self._id_factory,
            ))
            embedding = OpenAIEmbeddingAdapter(
                self._http, api_key=openai.value.get_secret_value(),
                base_url=settings.openai_base_url,
            )
            return RunProviderBundle(workflow, stack, embedding, self._m8_runtime)
        except BaseException:
            await stack.aclose()
            raise
