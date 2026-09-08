from __future__ import annotations

from fastapi import FastAPI

from app.api.http.health import router as health_router
from app.infrastructure.config import AppSettings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved_settings = settings or AppSettings()
    application = FastAPI(title=resolved_settings.app_name)
    application.state.settings = resolved_settings
    application.include_router(health_router)
    return application


app = create_app()
