from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI

from app.api.http.health import router as health_router
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

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        coordinator = SessionCoordinator(
            clock if clock is not None else SystemClock(),
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

    application = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    application.state.settings = resolved_settings
    application.include_router(health_router)
    return application


app = create_app()
