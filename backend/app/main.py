from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Coroutine
from uuid import UUID, uuid4

from fastapi import FastAPI

from app.api.http.analysis_runs import REQUEST_ID_HEADERS, error_responses
from app.api.http.analysis_runs import router as analysis_runs_router
from app.api.http.errors import register_error_handlers
from app.api.http.health import router as health_router
from app.api.http.request_context import RequestContextMiddleware
from app.api.schemas.analysis_runs import CreateAnalysisRunRequest
from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.creation import RunCreationCandidate, RunCreationResult
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceWriteBatch,
)
from app.application.analysis_runs.startup_repair import repair_interrupted_runs
from app.application.analysis_runs.workflow import AnalysisWorkflow
from app.application.query_understanding import AnalysisContext
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import TranscriptRegistry, create_production_registry
from app.application.transcripts.stream_models import RunSnapshotEvent
from app.domain import HydrologyGraph
from app.domain.hydrology import ParameterCapabilityRegistry
from app.infrastructure.config import (
    AppSettings,
    ModelCatalog,
    load_app_settings,
    load_parameter_capability_registry,
)
from app.infrastructure.observability.json_logging import configure_logging
from app.infrastructure.persistence.database import (
    check_database_schema,
    create_database_engine,
)
from app.infrastructure.persistence.store import PostgresTranscriptStore
from app.infrastructure.runtime.system_clock import SystemClock
from app.infrastructure.runtime import (
    RuntimeComponents,
    build_m4_runtime,
    build_m5_runtime,
)
from app.ports.clock import ClockPort
from app.ports.model_catalog import ModelCatalogPort
from app.ports.transcript_store import TranscriptStorePort


@asynccontextmanager
async def _provided_runtime(
    workflow: AnalysisWorkflow,
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
    parameter_capability_registry: ParameterCapabilityRegistry | None = None,
    workflow_task_factory: Callable[
        [Coroutine[object, object, None], str],
        asyncio.Task[None],
    ] | None = None,
) -> FastAPI:
    resolved_settings = settings or load_app_settings()
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_model_catalog = model_catalog or ModelCatalog(resolved_settings.model_catalog)
    resolved_parameter_capabilities = (
        parameter_capability_registry or load_parameter_capability_registry()
    )
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        runtime_context = (
            _provided_runtime(workflow)
            if workflow is not None
            else build_m4_runtime(
                resolved_settings,
                clock=resolved_clock,
                parameter_capabilities=resolved_parameter_capabilities,
                error_id_factory=id_factory,
            )
        )
        async with runtime_context as runtime:
            engine = None
            coordinator = None
            try:
                active_store = transcript_store
                if active_store is None:
                    engine = create_database_engine(resolved_settings)
                    await check_database_schema(resolved_settings, engine=engine)
                    active_store = PostgresTranscriptStore(
                        engine,
                        operation_timeout_seconds=(
                            resolved_settings.db_operation_timeout_seconds
                        ),
                    )
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
                    workflow=runtime.workflow,
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
                )
                transcript_reader = TranscriptReader(
                    coordinator,
                    active_store,
                    active_registry,
                    error_id_factory=id_factory,
                    inline_content_bytes=resolved_settings.transcript_inline_max_bytes,
                    preview_bytes=resolved_settings.transcript_preview_bytes // 2,
                )
                async with build_m5_runtime(
                    resolved_settings,
                    operations=coordinator,
                    transcript_reader=transcript_reader,
                    capability_registry=resolved_parameter_capabilities,
                    clock=resolved_clock,
                ) as m5_runtime:
                    application.state.run_coordinator = coordinator
                    application.state.transcript_reader = transcript_reader
                    application.state.transcript_store = active_store
                    application.state.m5_runtime = m5_runtime
                    yield
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
    application.add_middleware(RequestContextMiddleware, id_factory=id_factory)
    register_error_handlers(application)
    application.include_router(
        health_router, responses={**error_responses(405, 500), 200: {"headers": REQUEST_ID_HEADERS}}
    )
    application.include_router(analysis_runs_router)

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
        for public_model in (AnalysisContext, HydrologyGraph, RunSnapshotEvent):
            public_schema = public_model.model_json_schema(
                ref_template="#/components/schemas/{model}",
            )
            schemas.update(public_schema.pop("$defs", {}))
            schemas[public_model.__name__] = public_schema
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
        return schema

    application.openapi = openapi_with_create_body
    return application


app = create_app()
