import asyncio
import importlib
import json
import socket
import sys
import threading
from zoneinfo import TZPATH, ZoneInfo, reset_tzpath

import httpx
import pytest
from pydantic import ValidationError

from app.infrastructure.config import AppSettings
from tests.m4_support import create_test_app as create_app


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
        "model_catalog": (),
        "memory_enabled": True,
        "topology_max_distance_km": 500.0,
        "topology_max_hops": 48,
        "topology_max_nodes": 128,
        "topology_max_concurrency": 8,
        "nldi_request_interval_seconds": 1.5,
        "waterbody_workflow_max_concurrency": 4,
        "prediction_baseline_hours": 48,
        "max_target_parameters": 6,
        "query_parser_timeout_seconds": 45.0,
        "geocoding_timeout_seconds": 10.0,
        "metadata_timeout_seconds": 15.0,
        "provider_http_max_response_bytes": 4194304,
        "provider_http_page_size": 1000,
        "provider_http_max_pages": 10,
        "provider_http_max_records": 10000,
        "provider_http_retry_delay_seconds": 0.25,
        "evidence_provider_timeout_seconds": 20.0,
        "evidence_provider_attempts": 2,
        "evidence_total_deadline_seconds": 180.0,
        "evidence_max_collection_calls": 12,
        "evidence_max_transform_calls": 24,
        "evidence_max_pages": 10,
        "evidence_max_records": 10000,
        "evidence_max_response_bytes": 8388608,
        "evidence_inspection_page_size": 50,
        "evidence_inspection_max_page_size": 200,
        "evidence_preview_max_records": 32,
        "evidence_preview_max_bytes": 16384,
        "evidence_facility_max_centers": 32,
        "reasoning_max_planning_turns": 6,
        "reasoning_max_tool_calls_per_turn": 4,
        "reasoning_max_cells_per_batch": 32,
        "reasoning_max_batches": 24,
        "reasoning_model_timeout_seconds": 90.0,
        "reasoning_session_deadline_seconds": 600.0,
        "run_summary_timeout_seconds": 120.0,
        "nws_user_agent_project": "WaterAgents",
        "nws_user_agent_contact": "wateragents@example.invalid",
        "google_geocoding_api_key": None,
        "usgs_api_key": None,
        "openai_api_key": None,
        "openai_base_url": "https://api.openai.com/v1",
        "topology_stage_timeout_seconds": 1200.0,
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
        "rag_discovery_batch_size": 64,
        "rag_reconciliation_max_batches": 4,
        "rag_worker_claim_batch_size": 16,
        "rag_embedding_batch_size": 16,
        "rag_worker_max_concurrency": 1,
        "rag_embedding_timeout_seconds": 30.0,
        "rag_job_lease_seconds": 60.0,
        "rag_job_backoff_base_seconds": 1.0,
        "rag_job_backoff_max_seconds": 60.0,
        "rag_reconciliation_interval_seconds": 30.0,
        "rag_worker_shutdown_grace_seconds": 5.0,
        "rag_retired_generation_retention_seconds": 604800.0,
    }
    assert create_app(settings).state.settings is settings


@pytest.mark.parametrize(
    "values",
    (
        {"rag_worker_claim_batch_size": 1, "rag_embedding_batch_size": 2},
        {"rag_embedding_timeout_seconds": 30, "rag_job_lease_seconds": 30},
        {"rag_job_backoff_base_seconds": 2, "rag_job_backoff_max_seconds": 1},
    ),
)
def test_m8_worker_settings_reject_incoherent_budgets(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **values)


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


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("topology_max_distance_km", 0),
        ("topology_max_hops", 0),
        ("topology_max_nodes", 10001),
        ("topology_max_concurrency", 65),
        ("nldi_request_interval_seconds", float("nan")),
        ("nldi_request_interval_seconds", -0.1),
        ("prediction_baseline_hours", 169),
        ("max_target_parameters", 7),
        ("query_parser_timeout_seconds", float("inf")),
        ("geocoding_timeout_seconds", 0),
        ("metadata_timeout_seconds", 121),
        ("provider_http_max_response_bytes", 1023),
        ("provider_http_page_size", 50001),
        ("provider_http_max_pages", 101),
        ("provider_http_max_records", 50001),
        ("provider_http_retry_delay_seconds", float("nan")),
        ("topology_stage_timeout_seconds", 3601),
    ],
)
def test_m4_hard_limits_reject_out_of_range_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        AppSettings(**{field_name: value})


def test_m4_hard_limits_are_strict_but_environment_values_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="topology_max_hops"):
        AppSettings(topology_max_hops="12.5")

    monkeypatch.setenv("WATERAGENTS_TOPOLOGY_MAX_HOPS", "13")
    assert AppSettings(_env_file=None).topology_max_hops == 13


def test_provider_http_page_size_cannot_exceed_record_limit() -> None:
    with pytest.raises(
        ValidationError,
        match="provider_http_page_size must not exceed",
    ):
        AppSettings(
            provider_http_page_size=100,
            provider_http_max_records=10,
        )


def test_provider_secrets_remain_masked_and_preserve_whitespace() -> None:
    secret = "  provider-secret  "
    settings = AppSettings(
        google_geocoding_api_key=secret,
        usgs_api_key=secret,
        openai_api_key=secret,
    )

    assert settings.google_geocoding_api_key is not None
    assert settings.usgs_api_key is not None
    assert settings.openai_api_key is not None
    assert settings.google_geocoding_api_key.get_secret_value() == secret
    assert settings.usgs_api_key.get_secret_value() == secret
    assert settings.openai_api_key.get_secret_value() == secret
    assert secret not in repr(settings)


@pytest.mark.parametrize(
    "value",
    [
        "api.openai.com/v1",
        "https://",
        "https://user:secret@api.openai.com/v1",
        "https://api.openai.com/v1?key=secret",
        "https://api.openai.com/v1#fragment",
    ],
)
def test_openai_base_url_rejects_unsafe_or_incomplete_values(value: str) -> None:
    with pytest.raises(ValidationError, match="openai_base_url"):
        AppSettings(openai_base_url=value)


def test_model_catalog_is_parsed_once_as_safe_role_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WATERAGENTS_MODEL_CATALOG",
        json.dumps(
            [
                {
                    "provider": "configured-provider",
                    "model": "configured-model",
                    "roles": ["query_parser", "waterbody"],
                    "context_window_tokens": 64_000,
                    "max_output_tokens": 8_192,
                },
                {
                    "provider": "configured-provider",
                    "model": "embedding-model",
                    "roles": ["embedding"],
                    "embedding_profile": {
                        "provider": "configured-provider",
                        "model": "embedding-model",
                        "output_dimensions": 8,
                        "max_input_tokens": 512,
                        "distance_metric": "cosine",
                        "semantic_min_similarity": 0.78,
                        "canonical_query_schema_version": 1,
                        "canonical_template_version": 1,
                        "projection_schema_version": 1,
                    },
                },
            ]
        ),
    )

    capabilities = AppSettings(_env_file=None).model_catalog
    capability = capabilities[0]
    assert capability.model_dump(mode="json") == {
        "provider": "configured-provider",
        "model": "configured-model",
        "roles": ["query_parser", "waterbody"],
        "context_window_tokens": 64_000,
        "max_output_tokens": 8_192,
        "embedding_profile": None,
    }
    embedding = capabilities[1]
    assert embedding.embedding_profile is not None
    assert embedding.embedding_profile.output_dimensions == 8
    assert embedding.embedding_profile.max_input_tokens == 512
    assert embedding.embedding_profile.distance_metric.value == "cosine"
    assert embedding.embedding_profile.semantic_min_similarity == 0.78
    assert "secret" not in embedding.model_dump_json().casefold()


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


@pytest.mark.parametrize(
    "field_name",
    ("nws_user_agent_project", "nws_user_agent_contact"),
)
def test_nws_user_agent_settings_reject_header_injection(field_name: str) -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, **{field_name: "value\r\nX-Secret: no"})


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
    assert configurations[0].loop == (
        "app.infrastructure.runtime.event_loop:windows_selector_loop_factory"
        if sys.platform == "win32"
        else "auto"
    )
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
