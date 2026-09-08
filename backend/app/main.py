from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI

from app.api.http.analysis_sessions import REQUEST_ID_HEADERS, error_responses
from app.api.http.analysis_sessions import router as analysis_sessions_router
from app.api.http.errors import register_error_handlers
from app.api.http.health import router as health_router
from app.api.http.request_context import RequestContextMiddleware
from app.application.analysis_sessions.coordinator import SessionCoordinator
from app.application.analysis_sessions.workflow import AnalysisWorkflow
from app.infrastructure.config import AppSettings
from app.infrastructure.runtime.system_clock import SystemClock
from app.ports.clock import ClockPort


def create_app(
    settings: AppSettings | None = None,
    *,
    clock: ClockPort | None = None,
    workflow: AnalysisWorkflow | None = None,
    id_factory: Callable[[], UUID] = uuid4,
    shutdown_grace_period: float = 1.0,
    keepalive_interval: float = 15.0,
) -> FastAPI:
    resolved_settings = settings or AppSettings()
    resolved_clock = clock if clock is not None else SystemClock()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        coordinator = SessionCoordinator(
            resolved_clock,
            workflow=workflow,
            id_factory=id_factory,
            shutdown_grace_period=shutdown_grace_period,
            keepalive_interval=keepalive_interval,
        )
        application.state.session_coordinator = coordinator
        try:
            yield
        finally:
            await coordinator.shutdown()

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
    application.include_router(analysis_sessions_router)
    return application


app = create_app()
