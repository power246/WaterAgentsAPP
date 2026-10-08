import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.analysis_runs.query_topology_workflow import (
    QueryTopologyWorkflow,
)
from app.application.query_understanding import QueryParserWorkflow
from app.domain import EmbeddingProfile, ModelCapability, ModelRole
from app.infrastructure.adapters import OpenAIResponsesParserAdapter
from app.infrastructure.config import (
    AppSettings,
    SettingsConfigurationError,
    load_parameter_capability_registry,
)
from app.infrastructure.runtime import build_m4_runtime
from app.main import create_app


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module_name",
    (
        "app.application.analysis_runs.workflow",
        "app.infrastructure.runtime.event_loop",
        "app.infrastructure.runtime.m4",
    ),
)
def test_runtime_modules_import_in_fresh_interpreter(module_name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 27, tzinfo=UTC)


def catalog(provider: str = "openai") -> tuple[ModelCapability, ...]:
    embedding_profile = EmbeddingProfile(
        provider="openai",
        model="embedding-v1",
        output_dimensions=8,
        max_input_tokens=512,
        semantic_min_similarity=0.78,
    )
    return (
        ModelCapability(
            provider=provider,
            model="parser-v1",
            roles=(ModelRole.QUERY_PARSER,),
        ),
        ModelCapability(
            provider="openai",
            model="waterbody-v1",
            roles=(ModelRole.WATERBODY,),
            context_window_tokens=128_000,
            max_output_tokens=32_768,
        ),
        ModelCapability(
            provider=embedding_profile.provider,
            model=embedding_profile.model,
            roles=(ModelRole.EMBEDDING,),
            embedding_profile=embedding_profile,
        ),
    )


def settings(**overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "model_catalog": catalog(),
        "openai_api_key": "openai-secret",
        "google_geocoding_api_key": "google-secret",
        "topology_max_distance_km": 77.0,
        "topology_max_hops": 9,
        "topology_max_nodes": 55,
        "topology_max_concurrency": 3,
        "query_parser_timeout_seconds": 12.0,
        "geocoding_timeout_seconds": 7.0,
        "metadata_timeout_seconds": 8.0,
    }
    values.update(overrides)
    return AppSettings(_env_file=None, **values)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        (AppSettings(_env_file=None), "query_parser"),
        (
            settings(model_catalog=(catalog()[0],)),
            "waterbody",
        ),
        (settings(openai_api_key=None), "openai_api_key"),
        (settings(google_geocoding_api_key=None), "google_geocoding_api_key"),
        (settings(model_catalog=catalog("unsupported")), "unsupported"),
    ],
)
def test_production_runtime_rejects_incomplete_or_unsupported_configuration(
    configured: AppSettings,
    message: str,
) -> None:
    async def exercise() -> None:
        with pytest.raises(SettingsConfigurationError, match=message):
            async with build_m4_runtime(
                configured,
                clock=FixedClock(),
                parameter_capabilities=load_parameter_capability_registry(),
                error_id_factory=uuid4,
            ):
                raise AssertionError("invalid runtime was entered")

    run(exercise())


def test_runtime_factory_wires_real_m4_components_and_configured_limits() -> None:
    async def exercise() -> None:
        async with build_m4_runtime(
            settings(),
            clock=FixedClock(),
            parameter_capabilities=load_parameter_capability_registry(),
            error_id_factory=uuid4,
        ) as runtime:
            assert isinstance(runtime.workflow, QueryTopologyWorkflow)
            query = runtime.workflow._query_understanding
            topology = runtime.workflow._topology_builder
            assert isinstance(query, QueryParserWorkflow)
            assert isinstance(query._parser, OpenAIResponsesParserAdapter)
            assert query._timeout_seconds == 12.0
            assert query._parser._http._timeout_seconds == 12.0
            resolver = query._context_resolver
            location = resolver._location_resolver
            assert location._geocoding._http._timeout_seconds == 7.0
            assert location._comid_lookup._http._timeout_seconds == 8.0
            assert resolver._parameter_resolver._catalog._http._timeout_seconds == 8.0
            assert topology._limits.max_distance_km == 77.0
            assert topology._limits.max_hops == 9
            assert topology._limits.max_nodes == 55
            assert topology._max_concurrency == 3
            assert topology._stage_timeout_seconds == 120.0

    run(exercise())


def test_default_app_validates_database_before_m4_and_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TestEngine:
        async def dispose(self) -> None:
            events.append("database_disposed")

    def create_test_engine(*args, **kwargs):
        del args, kwargs
        events.append("database_created")
        return TestEngine()

    async def validate_test_database(*args, **kwargs) -> None:
        del args, kwargs
        events.append("database_validated")

    monkeypatch.setattr(
        "app.main.create_database_engine",
        create_test_engine,
    )
    monkeypatch.setattr(
        "app.main.check_database_schema",
        validate_test_database,
    )
    application = create_app(AppSettings(_env_file=None))

    async def exercise() -> None:
        with pytest.raises(SettingsConfigurationError, match="deployment settings catalog"):
            async with application.router.lifespan_context(application):
                raise AssertionError("misconfigured app started")

    run(exercise())
    assert events == [
        "database_created",
        "database_validated",
        "database_disposed",
    ]
