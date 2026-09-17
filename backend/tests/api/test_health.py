import asyncio
import importlib
import socket
import threading
from zoneinfo import TZPATH, ZoneInfo, reset_tzpath

import httpx
import pytest
from pydantic import ValidationError

from app.infrastructure.config import AppSettings
from app.main import create_app


def test_app_uses_expected_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in AppSettings.model_fields:
        monkeypatch.delenv(f"WATERAGENTS_{field_name.upper()}", raising=False)

    settings = AppSettings(_env_file=None)

    assert settings.model_dump() == {
        "app_name": "WaterAgents API",
        "environment": "development",
        "log_level": "info",
        "host": "127.0.0.1",
        "port": 8000,
        "analysis_timezone": "America/Chicago",
        "db_host": "127.0.0.1",
        "db_port": 5432,
        "db_name": "wateragents",
        "db_user": "wateragents",
        "db_password": None,
        "db_sslmode": "disable",
        "db_sslrootcert": None,
        "db_pool_size": 5,
        "db_max_overflow": 5,
        "db_operation_timeout_seconds": 5.0,
        "idempotency_wait_timeout_seconds": 5.0,
        "persistence_batch_items": 100,
        "transcript_inline_max_bytes": 65536,
        "transcript_preview_bytes": 2048,
        "live_state_grace_seconds": 60.0,
        "shutdown_save_grace_seconds": 5.0,
    }
    assert create_app(settings).state.settings is settings


def test_settings_use_wateragents_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_NAME", "ignored")
    monkeypatch.setenv("WATERAGENTS_APP_NAME", "Configured API")
    monkeypatch.setenv("WATERAGENTS_PORT", "9000")

    settings = AppSettings()

    assert settings.app_name == "Configured API"
    assert settings.port == 9000


@pytest.mark.parametrize(
    "log_level",
    ("trace", "debug", "info", "warning", "error", "critical"),
)
def test_settings_accept_supported_log_levels(log_level: str) -> None:
    assert AppSettings(log_level=log_level).log_level == log_level


def test_settings_normalize_log_level() -> None:
    assert AppSettings(log_level=" WARNING ").log_level == "warning"


def test_invalid_log_level_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATERAGENTS_LOG_LEVEL", "BOGUS")

    with pytest.raises(ValidationError, match="log_level"):
        AppSettings()


def test_invalid_analysis_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        AppSettings(analysis_timezone="Not/A-Timezone")


def test_timezone_can_load_without_system_zoneinfo() -> None:
    original_tzpath = TZPATH
    ZoneInfo.clear_cache()
    reset_tzpath(())
    try:
        settings = AppSettings()
        assert ZoneInfo(settings.analysis_timezone).key == "America/Chicago"
    finally:
        ZoneInfo.clear_cache()
        reset_tzpath(original_tzpath)


def test_health_returns_exact_response() -> None:
    async def request_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.parametrize("workers", ["4", "not-an-integer"])
def test_main_entrypoint_enforces_one_worker_despite_environment(monkeypatch, workers) -> None:
    import app.__main__ as entrypoint

    monkeypatch.setenv("WEB_CONCURRENCY", workers)
    monkeypatch.setenv("WATERAGENTS_DB_PASSWORD", "entrypoint-test-only")
    configurations = []

    def capture_run(app, **options):
        configurations.append(entrypoint.uvicorn.Config(app, log_config=None, **options))

    monkeypatch.setattr(entrypoint.uvicorn, "run", capture_run)
    entrypoint.main()
    assert len(configurations) == 1
    assert configurations[0].app == "app.main:app"
    assert configurations[0].loop == "auto"
    assert configurations[0].workers == 1


def test_import_has_no_file_network_or_thread_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import app.main as main_module

    def reject_network(*args, **kwargs):
        raise AssertionError("app import attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.chdir(tmp_path)
    files_before = tuple(tmp_path.rglob("*"))
    thread_ids_before = {thread.ident for thread in threading.enumerate()}

    importlib.reload(main_module)

    assert tuple(tmp_path.rglob("*")) == files_before
    assert {thread.ident for thread in threading.enumerate()} == thread_ids_before
