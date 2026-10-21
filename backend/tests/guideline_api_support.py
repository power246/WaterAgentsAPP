from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.m4_support import create_test_app
from tests.guideline_bundle_support import EmptyBundledSource

GUIDELINE_METHODS = {
    "/api/guidelines": {"get", "post"},
    "/api/guidelines/{document_id}": {"get"},
    "/api/guidelines/{document_id}/draft": {"put"},
    "/api/guidelines/{document_id}/local-edit": {"post"},
    "/api/guidelines/{document_id}/publish": {"post"},
    "/api/guidelines/{document_id}/set-enabled": {"post"},
    "/api/guidelines/{document_id}/restore-bundled": {"post"},
    "/api/guidelines/operations/{operation_id}": {"get"},
    "/api/guidelines/operations/{operation_id}/retry": {"post"},
    "/api/guidelines/{document_id}/versions/{version_id}": {"get"},
}


@asynccontextmanager
async def guideline_client(settings, *, repository=None):
    engine = create_database_engine(settings)
    active_repository = repository if repository is not None else PostgresGuidelineRepository(settings, engine=engine)
    application = create_test_app(AppSettings(_env_file=None),
        transcript_store=InMemoryTranscriptStore(), guideline_repository=active_repository, shutdown_grace_period=0)
    try:
        with patch('app.infrastructure.runtime.guidelines.PackageBundledGuidelineSource',return_value=EmptyBundledSource()):
            async with application.router.lifespan_context(application):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000") as client:
                    yield application, client, active_repository
    finally:
        await engine.dispose()
