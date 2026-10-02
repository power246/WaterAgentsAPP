import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.evidence_tools import (
    EvidenceProducerStatus,
    EvidenceToolExecutor,
    EvidenceTranscriptStore,
)
from app.infrastructure.adapters import (
    EpaEchoFacilityAdapter,
    NwsWeatherAdapter,
    UsgsObservationAdapter,
)
from app.infrastructure.config import (
    AppSettings,
    SettingsConfigurationError,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import build_m5_runtime, compose_m5_runtime
from tests.m4_support import create_request_bytes, create_test_app


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 1, tzinfo=UTC)


class EmptyPort:
    async def collect(self, request, *, deadline=None):
        raise AssertionError((request, deadline))


def test_production_m5_module_import_does_not_reach_test_composition() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.infrastructure.runtime.m5; "
                "assert 'app.infrastructure.runtime.m5_test' not in sys.modules"
            ),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_composition_fails_closed_when_required_component_is_missing() -> None:
    with pytest.raises(SettingsConfigurationError, match="water_port"):
        compose_m5_runtime(
            AppSettings(_env_file=None),
            operations=object(),  # type: ignore[arg-type]
            transcript_reader=object(),  # type: ignore[arg-type]
            capability_registry=load_parameter_capability_registry(),
            water_port=None,  # type: ignore[arg-type]
            weather_port=EmptyPort(),
            facility_port=EmptyPort(),
            clock=FixedClock(),
        )


def test_production_builder_wires_real_adapters_and_keeps_future_producers_off() -> None:
    async def exercise() -> None:
        async with build_m5_runtime(
            AppSettings(
                _env_file=None,
                evidence_provider_attempts=1,
                evidence_max_pages=3,
            ),
            operations=object(),  # type: ignore[arg-type]
            transcript_reader=object(),  # type: ignore[arg-type]
            capability_registry=load_parameter_capability_registry(),
            clock=FixedClock(),
        ) as runtime:
            assert isinstance(runtime.executor, EvidenceToolExecutor)
            assert isinstance(runtime.transcript, EvidenceTranscriptStore)
            assert runtime.executor._writer is runtime.transcript
            assert runtime.executor._reader is runtime.transcript
            assert isinstance(runtime.executor._water_port, UsgsObservationAdapter)
            assert isinstance(runtime.executor._weather_port, NwsWeatherAdapter)
            assert isinstance(runtime.executor._facility_port, EpaEchoFacilityAdapter)
            assert runtime.executor._water_port._http._attempts == 1
            assert runtime.executor._facility_port._max_pages == 3
            assert runtime.executor._single_flight is not None
            assert (
                runtime.registry.get("get_upstream_results").producer_status
                is EvidenceProducerStatus.UNAVAILABLE
            )
            assert (
                runtime.registry.get("search_transcript_evidence").producer_status
                is EvidenceProducerStatus.UNAVAILABLE
            )

    asyncio.run(exercise())


def test_production_app_starts_with_internal_m5_runtime_and_no_invocation_route() -> None:
    application = create_test_app(
        settings=AppSettings(_env_file=None),
        transcript_store=InMemoryTranscriptStore(),
    )

    async def exercise() -> None:
        async with application.router.lifespan_context(application):
            runtime = application.state.m5_runtime
            assert isinstance(runtime.executor, EvidenceToolExecutor)
            assert runtime.acquisition._executor is runtime.executor
            assert runtime.acquisition._reader is runtime.transcript
            assert runtime.acquisition._writer is runtime.transcript

    asyncio.run(exercise())
    paths = set(application.openapi()["paths"])
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in ("/__test__", "/tools", "/evidence/collect")
    )


def test_app_shutdown_cleans_run_resources_before_m5_runtime_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as app_main

    runtime_open = False
    cleaned: list[UUID] = []

    async def cleanup_run(run_id: UUID) -> None:
        assert runtime_open
        cleaned.append(run_id)

    @asynccontextmanager
    async def fake_m5_runtime(*args, **kwargs):
        nonlocal runtime_open
        del args, kwargs
        runtime_open = True
        try:
            yield SimpleNamespace(
                acquisition=object(),
                cleanup_run=cleanup_run,
            )
        finally:
            runtime_open = False

    monkeypatch.setattr(app_main, "build_m5_runtime", fake_m5_runtime)
    application = create_test_app(
        settings=AppSettings(_env_file=None),
        transcript_store=InMemoryTranscriptStore(),
        shutdown_grace_period=0,
    )

    async def exercise() -> UUID:
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            created = await coordinator.create_run(
                create_request_bytes("cleanup on shutdown")
            )
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            return created.run_id

    run_id = asyncio.run(exercise())
    assert cleaned == [run_id]
    assert not runtime_open
