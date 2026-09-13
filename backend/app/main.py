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
from app.application.analysis_runs.workflow import AnalysisWorkflow
from app.infrastructure.config import AppSettings
from app.infrastructure.observability.json_logging import configure_logging
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.store import PostgresTranscriptStore
from app.infrastructure.runtime.system_clock import SystemClock
from app.ports.clock import ClockPort
from app.ports.transcript_store import TranscriptStorePort


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
    ] | None = None,
    workflow_task_factory: Callable[
        [Coroutine[object, object, None], str],
        asyncio.Task[None],
    ] | None = None,
) -> FastAPI:
    resolved_settings = settings or AppSettings()
    resolved_clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        engine = None
        active_store = transcript_store
        if active_store is None:
            engine = create_database_engine(resolved_settings)
            active_store = PostgresTranscriptStore(
                engine,
                operation_timeout_seconds=(
                    resolved_settings.db_operation_timeout_seconds
                ),
            )
        coordinator = RunCoordinator(
            resolved_clock,
            transcript_store=active_store,
            analysis_timezone=resolved_settings.analysis_timezone,
            workflow=workflow,
            id_factory=id_factory,
            record_id_factory=record_id_factory,
            shutdown_grace_period=shutdown_grace_period,
            keepalive_interval=keepalive_interval,
            idempotency_wait_timeout=(
                resolved_settings.idempotency_wait_timeout_seconds
            ),
            inline_content_bytes=resolved_settings.transcript_inline_max_bytes,
            preview_bytes=resolved_settings.transcript_preview_bytes // 2,
            workflow_task_factory=workflow_task_factory,
        )
        application.state.run_coordinator = coordinator
        application.state.transcript_store = active_store
        try:
            yield
        finally:
            await coordinator.shutdown()
            if engine is not None:
                await engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name, lifespan=lifespan, redirect_slashes=False
    )
    application.state.settings = resolved_settings
    application.state.clock = resolved_clock
    application.state.id_factory = id_factory
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
        schemas.update(create_schema.pop("$defs", {}))
        schemas["CreateAnalysisRunRequest"] = create_schema
        return schema

    application.openapi = openapi_with_create_body
    return application


app = create_app()
