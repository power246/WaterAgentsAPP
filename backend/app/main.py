from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncContextManager, Coroutine
from uuid import UUID, uuid4

from fastapi import FastAPI, Request

from app.api.http.analysis_runs import REQUEST_ID_HEADERS, error_responses
from app.api.http.analysis_runs import router as analysis_runs_router
from app.api.http.errors import register_error_handlers
from app.api.http.health import router as health_router
from app.api.http.settings import router as settings_router
from app.api.http.admin import router as admin_router
from app.api.http.keys import router as keys_router
from app.api.http.guidelines import router as guidelines_router
from app.application.guidelines.authoring import GuidelineAuthoringService
from app.api.http.request_context import RequestContextMiddleware
from app.api.http.local_mutation import LocalMutationGuardMiddleware, _loopback
from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.api.schemas.settings_admin import M9_PUBLIC_SCHEMAS
from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.creation import RunCreationCandidate, RunCreationResult
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceWriteBatch,
)
from app.application.analysis_runs.startup_repair import repair_interrupted_runs
from app.application.settings_admin.errors import M9_PUBLIC_ERRORS
from app.application.settings_admin.runtime_settings import RuntimeSettingsResolver
from app.application.settings_admin.settings_service import (
    SettingsService, deployment_defaults,
)
from app.application.settings_admin.unit_policy_service import UnitPolicyService
from app.application.settings_admin.admin_service import AdminService
from app.application.settings_admin.admin_operation_runner import AdminOperationRunner
from app.application.settings_admin.credential_service import (
    CredentialService, CredentialTester,
)
from app.application.analysis_runs.workflow import AnalysisWorkflow
from app.application.query_understanding import AnalysisContext
from app.application.run_summary.models import RunSummaryResult
from app.application.evidence_tools import (
    RunLocalUpstreamResultRegistry,
    TranscriptUpstreamInferenceResultReader,
)
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import TranscriptRegistry, create_production_registry
from app.application.transcripts.stream_models import (
    RunSnapshotEvent, SessionProcessEvent, StreamEndEvent,
)
from app.domain import HydrologyGraph, InferenceResult
from app.domain.hydrology import ParameterCapabilityRegistry
from app.infrastructure.config import (
    AppSettings,
    ModelCatalog,
    SettingsConfigurationError,
    load_app_settings,
    load_parameter_capability_registry,
)
from app.infrastructure.observability.json_logging import configure_logging
from app.infrastructure.persistence.database import (
    check_database_schema,
    create_database_engine,
)
from app.infrastructure.persistence.store import PostgresTranscriptStore
from app.infrastructure.persistence.tombstone_repository import PostgresTombstoneRepository
from app.infrastructure.persistence.admin_operation_repository import PostgresAdminOperationRepository
from app.infrastructure.persistence.settings_repository import PostgresSettingsRepository
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.infrastructure.persistence.memory_settings import InMemorySettingsRepository
from app.infrastructure.credentials import OSKeyStore
from app.infrastructure.credentials.memory_key_store import (
    InMemoryKeyStore, OfflineCredentialTester,
)
from app.infrastructure.credentials.tester import ProviderCredentialTester
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.runtime.provider_bundle import RunProviderBundleFactory
from app.infrastructure.runtime.guidelines import GuidelineRuntime
from app.infrastructure.runtime.system_clock import SystemClock
from app.infrastructure.web.frontend import FrontendResponder
from app.infrastructure.runtime import (
    M6Runtime,
    M8Runtime,
    RuntimeComponents,
    build_m5_runtime,
    build_m6_runtime,
    build_m8_runtime,
    compose_m7_execution_stage,
    safe_m6_metadata,
)
from app.ports.clock import ClockPort
from app.ports.model_catalog import ModelCatalogPort
from app.ports.transcript_store import TranscriptStorePort
from app.ports.settings_store import SettingsStorePort
from app.ports.key_store import KeyStorePort
from app.ports.guidelines import GuidelineRepositoryPort
from app.domain.settings_admin import CredentialName


@asynccontextmanager
async def _provided_runtime(
    workflow: AnalysisWorkflow | None,
) -> AsyncIterator[RuntimeComponents]:
    yield RuntimeComponents(workflow=workflow)


def create_app(
    settings: AppSettings | None = None,
    *,
    clock: ClockPort | None = None,
    workflow: AnalysisWorkflow | None = None,
    id_factory: Callable[[], UUID] = uuid4,
    record_id_factory: Callable[[], UUID] = uuid4,
    shutdown_grace_period: float = 1.0,
    keepalive_interval: float = 15.0,
    transcript_store: TranscriptStorePort[
        RunCreationCandidate,
        RunCreationResult,
        PersistenceWriteBatch,
        BatchVerification,
    ] | None = None,
    transcript_registry: TranscriptRegistry | None = None,
    model_catalog: ModelCatalogPort | None = None,
    settings_store: SettingsStorePort | None = None,
    key_store: KeyStorePort | None = None,
    credential_tester: CredentialTester | None = None,
    parameter_capability_registry: ParameterCapabilityRegistry | None = None,
    workflow_task_factory: Callable[
        [Coroutine[object, object, None], str],
        asyncio.Task[None],
    ] | None = None,
    m6_runtime_builder: (
        Callable[..., AsyncContextManager[M6Runtime]] | None
    ) = None,
    m8_runtime_builder: (
        Callable[..., AsyncContextManager[M8Runtime]] | None
    ) = None,
    frontend_dist: Path | None = None,
    guideline_repository: GuidelineRepositoryPort | None = None,
) -> FastAPI:
    resolved_settings = settings or load_app_settings()
    if not _loopback(resolved_settings.host):
        raise SettingsConfigurationError("local API requires a loopback bind host")
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_model_catalog = model_catalog or ModelCatalog(resolved_settings.model_catalog)
    resolved_parameter_capabilities = (
        parameter_capability_registry or load_parameter_capability_registry()
    )
    frontend_responder = FrontendResponder(frontend_dist) if frontend_dist is not None else FrontendResponder()
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        logging.getLogger("wateragents.startup").info(
            "Frontend availability: %s.",
            "available" if frontend_responder.available else "unavailable",
        )
        engine = None
        coordinator = None
        m8_runtime = None
        admin_runner = None
        try:
            active_store = transcript_store
            foundation_validated = False
            if active_store is None:
                engine = create_database_engine(resolved_settings)
                await check_database_schema(resolved_settings, engine=engine)
                foundation_validated = True
                active_store = PostgresTranscriptStore(
                    engine,
                    operation_timeout_seconds=(
                        resolved_settings.db_operation_timeout_seconds
                    ),
                )
            elif m8_runtime_builder is None:
                raise SettingsConfigurationError(
                    "a custom transcript store requires an explicit M8 runtime builder"
                )
            active_settings_store = settings_store or (
                PostgresSettingsRepository(
                    engine, resolved_parameter_capabilities,
                    operation_timeout_seconds=resolved_settings.db_operation_timeout_seconds,
                )
                if engine is not None else InMemorySettingsRepository(
                    resolved_parameter_capabilities
                )
            )
            active_guideline_repository = guideline_repository
            if active_guideline_repository is None and engine is not None:
                active_guideline_repository = PostgresGuidelineRepository(
                    resolved_settings, engine=engine, registry=resolved_parameter_capabilities, clock=resolved_clock)
            application.state.guideline_service = GuidelineAuthoringService(
                active_guideline_repository, resolved_parameter_capabilities, resolved_clock, id_factory=id_factory)
            try:
                initial_defaults = deployment_defaults(
                    resolved_settings.analysis_timezone,
                    resolved_model_catalog.snapshot(),
                )
                settings_service = SettingsService(
                    active_settings_store, resolved_model_catalog.snapshot(),
                    initial_defaults, resolved_clock,
                    unit_registry=resolved_parameter_capabilities,
                    memory_enabled=resolved_settings.memory_enabled,
                )
            except ValueError as error:
                raise SettingsConfigurationError(
                    "deployment settings catalog is invalid"
                ) from error
            settings_resolver = RuntimeSettingsResolver(settings_service)
            unit_policy_service = UnitPolicyService(
                settings=settings_service, store=active_settings_store,
                registry=resolved_parameter_capabilities, clock=resolved_clock,
            )
            active_key_store = key_store or (
                OSKeyStore() if engine is not None else InMemoryKeyStore()
            )
            active_tester = credential_tester or (
                ProviderCredentialTester(openai_base_url=resolved_settings.openai_base_url)
                if engine is not None else OfflineCredentialTester()
            )
            credential_service = CredentialService(
                active_key_store,
                {
                    CredentialName.OPENAI: resolved_settings.openai_api_key,
                    CredentialName.GOOGLE_GEOCODING: resolved_settings.google_geocoding_api_key,
                },
                resolved_clock, active_tester,
            )
            async with AsyncExitStack() as stack:
                # Provider auth belongs to adapters created for one run. The
                # application-owned pool has no default credential headers.
                provider_http = (
                    await stack.enter_async_context(ProviderHttpClient(
                        timeout_seconds=max(
                            resolved_settings.query_parser_timeout_seconds,
                            resolved_settings.geocoding_timeout_seconds,
                            resolved_settings.metadata_timeout_seconds,
                            resolved_settings.reasoning_model_timeout_seconds,
                            resolved_settings.run_summary_timeout_seconds,
                            resolved_settings.rag_embedding_timeout_seconds,
                            resolved_settings.guideline_policy.worker_timeout_seconds,
                        ),
                        max_response_bytes=resolved_settings.provider_http_max_response_bytes,
                        retry_delay_seconds=resolved_settings.provider_http_retry_delay_seconds,
                        attempts=2,
                    )) if workflow is None else None
                )
                runtime = await stack.enter_async_context(_provided_runtime(workflow))
                guideline_runtime = GuidelineRuntime(
                    settings=resolved_settings, catalog=resolved_model_catalog.snapshot(),
                    credentials=credential_service, http=provider_http, clock=resolved_clock,
                )
                provider_bundle_factory: RunProviderBundleFactory | None = None

                async def build_run_bundle(configuration):
                    if provider_bundle_factory is None:
                        raise RuntimeError("provider bundle factory is not ready")
                    return await provider_bundle_factory.build(configuration)
                active_registry = transcript_registry or create_production_registry()
                await repair_interrupted_runs(
                    active_store,
                    active_registry,
                    resolved_clock,
                    id_factory=record_id_factory,
                    inline_content_bytes=resolved_settings.transcript_inline_max_bytes,
                    preview_bytes=resolved_settings.transcript_preview_bytes // 2,
                )
                coordinator = RunCoordinator(
                    resolved_clock,
                    transcript_store=active_store,
                    model_catalog=resolved_model_catalog,
                    analysis_timezone=resolved_settings.analysis_timezone,
                    settings_resolver=settings_resolver,
                    workflow=runtime.workflow,
                    workflow_factory=build_run_bundle if workflow is None else None,
                    id_factory=id_factory,
                    record_id_factory=record_id_factory,
                    shutdown_grace_period=shutdown_grace_period,
                    shutdown_save_grace_period=(
                        resolved_settings.shutdown_save_grace_seconds
                    ),
                    live_state_grace_period=resolved_settings.live_state_grace_seconds,
                    keepalive_interval=keepalive_interval,
                    idempotency_wait_timeout=(
                        resolved_settings.idempotency_wait_timeout_seconds
                    ),
                    inline_content_bytes=resolved_settings.transcript_inline_max_bytes,
                    preview_bytes=resolved_settings.transcript_preview_bytes // 2,
                    persistence_batch_items=resolved_settings.persistence_batch_items,
                    persistence_attempt_timeout=(
                        resolved_settings.db_operation_timeout_seconds
                    ),
                    workflow_task_factory=workflow_task_factory,
                    transcript_registry=active_registry,
                    tombstone_lookup=(
                        PostgresTombstoneRepository(
                            engine,
                            operation_timeout_seconds=resolved_settings.db_operation_timeout_seconds,
                        ).get if engine is not None else None
                    ),
                )
                transcript_reader = TranscriptReader(
                    coordinator,
                    active_store,
                    active_registry,
                    error_id_factory=id_factory,
                    inline_content_bytes=resolved_settings.transcript_inline_max_bytes,
                    preview_bytes=resolved_settings.transcript_preview_bytes // 2,
                )
                query_topology_workflow = runtime.query_topology_workflow
                result_reader = TranscriptUpstreamInferenceResultReader(
                    transcript_reader, coordinator
                )
                upstream_results = RunLocalUpstreamResultRegistry(result_reader)
                if engine is None and m8_runtime_builder is None:
                    raise SettingsConfigurationError(
                        "production M8 runtime requires the PostgreSQL engine"
                    )
                active_m8_builder = m8_runtime_builder or build_m8_runtime
                m8_options = (
                    {"credential_service": credential_service, "provider_http": provider_http}
                    if workflow is None and m8_runtime_builder is None else {}
                )
                m8_runtime = await stack.enter_async_context(active_m8_builder(
                    resolved_settings,
                    engine=engine,
                    transcript_reader=transcript_reader,
                    transcript_registry=active_registry,
                    model_catalog=resolved_model_catalog.snapshot(),
                    clock=resolved_clock,
                    foundation_validated=foundation_validated,
                    **m8_options,
                ))
                try:
                    coordinator.register_run_terminal_persisted_hook(
                        m8_runtime.notify_run_terminal
                    )
                    m5_options = (
                        {"upstream_results": upstream_results}
                        if upstream_results is not None
                        else {}
                    )
                    m5_runtime = await stack.enter_async_context(
                        build_m5_runtime(
                            resolved_settings,
                            operations=coordinator,
                            transcript_reader=transcript_reader,
                            capability_registry=resolved_parameter_capabilities,
                            clock=resolved_clock,
                            transcript_port=m8_runtime.producer,
                            **m5_options,
                        )
                    )
                    coordinator.register_run_cleanup_hook(m5_runtime.cleanup_run)
                    if workflow is None:
                        assert provider_http is not None
                        provider_bundle_factory = RunProviderBundleFactory(
                            settings=resolved_settings, credentials=credential_service,
                            http=provider_http, operations=coordinator,
                            m5_runtime=m5_runtime, m8_runtime=m8_runtime,
                            guideline_runtime=guideline_runtime,
                            upstream_results=upstream_results,
                            result_reader=result_reader,
                            capability_registry=resolved_parameter_capabilities,
                            model_catalog=resolved_model_catalog.snapshot(),
                            clock=resolved_clock, id_factory=id_factory,
                            unit_policy_store=active_settings_store,
                        )
                        m6_runtime = None
                    else:
                        active_m6_builder = m6_runtime_builder or build_m6_runtime
                        m6_runtime = await stack.enter_async_context(
                            active_m6_builder(
                                resolved_settings,
                                operations=coordinator,
                                m5_runtime=m5_runtime,
                                capability_registry=resolved_parameter_capabilities,
                                model_catalog=resolved_model_catalog.snapshot(),
                                clock=resolved_clock,
                                operation_id_factory=id_factory,
                                result_id_factory=lambda: str(id_factory()),
                                error_id_factory=id_factory,
                            )
                        )
                    if query_topology_workflow is not None and m6_runtime is not None:
                        query_topology_workflow.bind_execution_stage(
                            compose_m7_execution_stage(
                                resolved_settings,
                                operations=coordinator,
                                m5_runtime=m5_runtime,
                                m6_runtime=m6_runtime,
                                upstream_results=upstream_results,
                                result_reader=result_reader,
                                capability_registry=resolved_parameter_capabilities,
                                model_catalog=resolved_model_catalog.snapshot(),
                                clock=resolved_clock,
                                result_id_factory=lambda: str(id_factory()),
                                error_id_factory=id_factory,
                                operation_id_factory=id_factory,
                            )
                        )
                    await m8_runtime.start()
                    await guideline_runtime.start(active_guideline_repository, resolved_parameter_capabilities)
                    admin_service = None
                    if engine is not None and isinstance(m8_runtime, M8Runtime):
                        profile_by_id = {
                            item.embedding_profile.profile_id: item.embedding_profile
                            for item in resolved_model_catalog.snapshot().capabilities
                            if item.embedding_profile is not None
                        }
                        operation_store = PostgresAdminOperationRepository(
                            engine,
                            operation_timeout_seconds=resolved_settings.db_operation_timeout_seconds,
                        )
                        async def credential_store_available() -> bool:
                            statuses = await credential_service.statuses()
                            return all(item.store_available for item in statuses)

                        admin_service = AdminService(
                            settings=settings_service,
                            maintenance=m8_runtime.operations,
                            store=operation_store, clock=resolved_clock,
                            profile_for_id=profile_by_id.__getitem__,
                            apply_profile=m8_runtime.apply_profile_handoff,
                            notify_maintenance=m8_runtime.notify_admin_maintenance,
                            credential_store_available=credential_store_available,
                            history_cleanup_guard=coordinator.history_cleanup_guard,
                            id_factory=id_factory,
                        )
                        admin_runner = AdminOperationRunner(
                            store=operation_store, clock=resolved_clock,
                            handle=admin_service.handle,
                        )
                        admin_service.bind_runner(admin_runner.schedule)
                        await admin_runner.start()
                    application.state.run_coordinator = coordinator
                    application.state.settings_service = settings_service
                    application.state.unit_policy_service = unit_policy_service
                    application.state.credential_service = credential_service
                    application.state.transcript_reader = transcript_reader
                    application.state.transcript_store = active_store
                    application.state.m8_runtime = m8_runtime
                    application.state.guideline_runtime = guideline_runtime
                    application.state.admin_service = admin_service
                    application.state.admin_runner = admin_runner
                    application.state.m5_runtime = m5_runtime
                    application.state.m6_runtime = m6_runtime
                    application.state.provider_http = provider_http
                    application.state.provider_bundle_factory = provider_bundle_factory
                    yield
                finally:
                    try:
                        try:
                            await guideline_runtime.shutdown()
                        finally:
                            if admin_runner is not None:
                                await admin_service.drain_accepts()
                                await admin_runner.shutdown()
                            await m8_runtime.shutdown()
                    finally:
                        try:
                            await coordinator.shutdown()
                        finally:
                            coordinator = None
                            m8_runtime = None
        finally:
            if coordinator is not None:
                await coordinator.shutdown()
            if engine is not None:
                await engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name, lifespan=lifespan, redirect_slashes=False
    )
    application.state.settings = resolved_settings
    application.state.clock = resolved_clock
    application.state.id_factory = id_factory
    application.state.parameter_capability_registry = resolved_parameter_capabilities
    application.state.frontend_responder = frontend_responder
    application.state.guideline_service = GuidelineAuthoringService(
        guideline_repository, resolved_parameter_capabilities, resolved_clock, id_factory=id_factory)
    application.add_middleware(
        LocalMutationGuardMiddleware,
        bind_host=resolved_settings.host, bind_port=resolved_settings.port,
    )
    application.add_middleware(RequestContextMiddleware, id_factory=id_factory)
    register_error_handlers(application)
    application.include_router(
        health_router, responses={**error_responses(405, 500), 200: {"headers": REQUEST_ID_HEADERS}}
    )
    application.include_router(analysis_runs_router)
    application.include_router(settings_router)
    application.include_router(keys_router)
    application.include_router(admin_router)
    application.include_router(guidelines_router)

    async def frontend_navigation(request: Request):
        return await frontend_responder.respond(request, request.path_params.get("full_path", ""))

    application.add_api_route("/", frontend_navigation, methods=["GET", "HEAD"], include_in_schema=False)
    application.add_api_route("/{full_path:path}", frontend_navigation, methods=["GET", "HEAD"], include_in_schema=False)

    generated_openapi = application.openapi

    def openapi_with_create_body() -> dict[str, object]:
        schema = generated_openapi()
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        create_schema = CreateAnalysisRunRequest.model_json_schema(
            ref_template="#/components/schemas/{model}",
        )
        definitions = create_schema.pop("$defs", {})
        for optional_object in ("target", "date_range"):
            create_schema["properties"][optional_object].pop("default", None)
        for optional_target_member in ("comid", "coordinates"):
            definitions["AnalysisTargetV1"]["properties"][
                optional_target_member
            ].pop("default", None)
        schemas.update(definitions)
        schemas["CreateAnalysisRunRequest"] = create_schema
        for public_model in (
            AnalysisContext, HydrologyGraph, InferenceResult, RunSummaryResult,
            RunSnapshotEvent,
            SessionProcessEvent, StreamEndEvent, *M9_PUBLIC_SCHEMAS,
        ):
            public_schema = public_model.model_json_schema(
                ref_template="#/components/schemas/{model}",
            )
            schemas.update(public_schema.pop("$defs", {}))
            schemas[public_model.__name__] = public_schema
        schema["x-m9-frontend-type-source"] = {
            "openapi": "/openapi.json",
            "sse_event_payloads": {
                "run_snapshot": "#/components/schemas/RunSnapshotEvent",
                "transcript_item": "#/components/schemas/TranscriptItemPayload",
                "message_started": "#/components/schemas/MessageStartedPayload",
                "message_delta": "#/components/schemas/MessageDeltaPayload",
                "message_finalized": "#/components/schemas/MessageFinalizedPayload",
                "stream_end": "#/components/schemas/StreamEndEvent",
            },
            "session_envelope": "#/components/schemas/SessionProcessEvent",
        }
        schema["x-m9-public-errors"] = [
            {"code": error.code, "status": error.status, "retryable": error.retryable}
            for error in M9_PUBLIC_ERRORS
        ]
        schema["x-m4-public-errors"] = [
            {"code": code, "failed_stage": stage, "retryable": retryable}
            for code, stage, retryable in (
                ("query_parse_failed", "query_parsing", False),
                ("query_clarification_required", "query_resolution", False),
                ("location_required", "location_resolution", False),
                ("location_ambiguous", "location_resolution", False),
                ("location_conflict", "location_resolution", False),
                ("location_outside_supported_region", "location_resolution", False),
                ("location_not_supported", "location_resolution", False),
                ("target_mismatch", "location_resolution", False),
                ("date_range_required", "date_resolution", False),
                ("date_range_conflict", "date_resolution", False),
                ("date_range_out_of_bounds", "date_resolution", False),
                ("resolution_conflict", "date_resolution", False),
                ("parameter_required", "parameter_resolution", False),
                ("parameter_ambiguous", "parameter_resolution", False),
                ("parameter_unsupported", "parameter_resolution", False),
                ("too_many_parameters", "parameter_resolution", False),
                ("parser_model_unavailable", "query_parsing", True),
                ("location_resolution_unavailable", "location_resolution", True),
                ("parameter_catalog_unavailable", "parameter_resolution", True),
                ("query_resolution_unavailable", "query_resolution", True),
                ("topology_lookup_failed", "topology_building", True),
                ("station_availability_failed", "topology_building", True),
                ("topology_invalid", "topology_building", False),
            )
        ]
        schema["x-m4-parameter-capability-baseline"] = {
            "count": len(resolved_parameter_capabilities.capabilities),
            "required_support": [
                capability.key.model_dump(mode="json")
                for capability in resolved_parameter_capabilities.required_support
            ],
            "max_target_parameters": 6,
        }
        schema["x-m6-runtime"] = safe_m6_metadata(
            resolved_settings,
            resolved_model_catalog.snapshot(),
        )
        return schema

    application.openapi = openapi_with_create_body
    return application


app = create_app()
