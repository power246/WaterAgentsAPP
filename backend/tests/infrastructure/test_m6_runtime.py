import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.reasoning import ModelSizeEstimate
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    WaterbodyFinishReason,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.domain import (
    ItemReference,
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
)
from app.infrastructure.config import (
    AppSettings,
    SettingsConfigurationError,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import (
    OpenAIResponsesSizeEstimator,
    WaterbodyAdapterRegistration,
    WaterbodyModelRouter,
    build_m6_runtime,
    compose_m6_runtime,
    evidence_tool_limits,
    m6_runtime_limits,
    safe_m6_metadata,
)
from app.infrastructure.runtime.m6_test import build_m6_test_runtime
from scripts import m6_provider_smoke
from tests.m4_support import (
    EMBEDDING_CAPABILITY,
    MODEL_CATALOG,
    WATERBODY_MODEL,
    _build_test_m6_runtime,
    create_test_app,
)


OPENAI_MODEL = ModelSelection(provider="openai", model="waterbody-v1")
FIXED_NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def run(coroutine):
    return asyncio.run(coroutine)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class RecordingPort:
    def __init__(self) -> None:
        self.calls: list[WaterbodyModelRequest] = []

    async def stream(self, request):
        self.calls.append(request)
        yield StructuredOutputCompleted(
            operation_id=request.operation_id,
            model=request.model,
            output_id="fixture-output",
            value='{"status":"ok"}',
        )
        yield Terminal(
            operation_id=request.operation_id,
            model=request.model,
            provider_response_id="fixture-response",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )


class FixedEstimator:
    def estimate(self, model, *, serialized_request, serialized_response_shape):
        del model, serialized_request, serialized_response_shape
        return ModelSizeEstimate(
            input_tokens=10,
            output_tokens=10,
            estimated_output_bytes=100,
        )


def catalog(*capabilities: ModelCapability) -> ModelCatalogSnapshot:
    return ModelCatalogSnapshot(
        capabilities=(*capabilities, EMBEDDING_CAPABILITY)
    )


def capability(
    selection: ModelSelection,
    *roles: ModelRole,
    context_window_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> ModelCapability:
    if ModelRole.WATERBODY in roles:
        if context_window_tokens is None:
            context_window_tokens = 128_000
        if max_output_tokens is None:
            max_output_tokens = 32_768
    values = {**selection.model_dump(), "roles": roles}
    if context_window_tokens is not None:
        values["context_window_tokens"] = context_window_tokens
    if max_output_tokens is not None:
        values["max_output_tokens"] = max_output_tokens
    return ModelCapability(**values)


def registration(provider: str, port=None) -> WaterbodyAdapterRegistration:
    return WaterbodyAdapterRegistration(
        provider=provider,
        port=port or RecordingPort(),
        size_estimator=FixedEstimator(),
    )


def request(selection: ModelSelection = OPENAI_MODEL) -> WaterbodyModelRequest:
    return WaterbodyModelRequest(
        operation_id=UUID("60000000-0000-4000-8000-000000000001"),
        phase=WaterbodyModelPhase.REASONING_BATCH,
        model=selection,
        system_prompt="fixture system prompt",
        messages=(
            WaterbodyModelMessage(
                message_id="fixture-message",
                role=WaterbodyMessageRole.USER,
                text="fixture request",
            ),
        ),
        response_schema=WaterbodyJsonSchema(
            name="fixture_response",
            schema_text=json.dumps(
                {
                    "type": "object",
                    "properties": {"status": {"const": "ok"}},
                    "required": ["status"],
                    "additionalProperties": False,
                }
            ),
        ),
        prompt_policy_digest="fixture-policy",
        scope_digest="fixture-scope",
        evidence_bundle_ref=ItemReference(item_id="fixture-bundle"),
        batch_id="fixture-batch",
        deadline=FIXED_NOW + timedelta(minutes=1),
    )


def composition_arguments(port=None):
    operations = object()
    acquisition = object()
    m5_runtime = SimpleNamespace(acquisition=acquisition)
    arguments = {
        "operations": operations,
        "m5_runtime": m5_runtime,
        "capability_registry": load_parameter_capability_registry(),
        "model_catalog": catalog(capability(OPENAI_MODEL, ModelRole.WATERBODY)),
        "registrations": (registration("openai", port),),
        "clock": FixedClock(),
    }
    return arguments


def test_m6_limit_defaults_and_derived_worst_case_are_fixed() -> None:
    settings = AppSettings(_env_file=None)
    limits = m6_runtime_limits(settings)

    assert (
        limits.planning_turns,
        limits.tool_calls_per_turn,
        limits.cells_per_batch,
        limits.batches,
        limits.model_timeout_seconds,
        limits.session_deadline_seconds,
    ) == (6, 4, 32, 24, 90.0, 600.0)
    assert limits.repairs_per_exchange == 1
    assert limits.maximum_model_calls == 6 + 2 * 24 + 2 == 56
    assert not any("repair" in field for field in AppSettings.model_fields)


def test_m6_limits_accept_lower_values_and_keep_m5_budgets_independent() -> None:
    settings = AppSettings(
        _env_file=None,
        reasoning_max_planning_turns=1,
        reasoning_max_tool_calls_per_turn=1,
        reasoning_max_cells_per_batch=1,
        reasoning_max_batches=1,
        reasoning_model_timeout_seconds=0.5,
        reasoning_session_deadline_seconds=1.0,
        evidence_max_collection_calls=2,
        evidence_max_transform_calls=3,
    )

    assert m6_runtime_limits(settings).maximum_model_calls == 5
    m5_limits = evidence_tool_limits(settings)
    assert m5_limits.collection_calls == 2
    assert m5_limits.transform_calls == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reasoning_max_planning_turns", 0),
        ("reasoning_max_planning_turns", 7),
        ("reasoning_max_tool_calls_per_turn", True),
        ("reasoning_max_tool_calls_per_turn", 1.5),
        ("reasoning_max_cells_per_batch", 33),
        ("reasoning_max_batches", 25),
        ("reasoning_model_timeout_seconds", True),
        ("reasoning_model_timeout_seconds", 0),
        ("reasoning_model_timeout_seconds", float("nan")),
        ("reasoning_session_deadline_seconds", float("inf")),
        ("reasoning_session_deadline_seconds", 601),
    ],
)
def test_m6_limits_reject_coercive_nonfinite_and_out_of_range_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match=field):
        AppSettings(_env_file=None, **{field: value})


def test_m6_limits_parse_integer_and_float_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "REASONING_MAX_PLANNING_TURNS": "5",
        "REASONING_MAX_TOOL_CALLS_PER_TURN": "3",
        "REASONING_MAX_CELLS_PER_BATCH": "16",
        "REASONING_MAX_BATCHES": "12",
        "REASONING_MODEL_TIMEOUT_SECONDS": "45.5",
        "REASONING_SESSION_DEADLINE_SECONDS": "300.5",
    }
    for name, value in values.items():
        monkeypatch.setenv(f"WATERAGENTS_{name}", value)

    settings = AppSettings(_env_file=None)

    assert settings.reasoning_max_planning_turns == 5
    assert settings.reasoning_max_tool_calls_per_turn == 3
    assert settings.reasoning_max_cells_per_batch == 16
    assert settings.reasoning_max_batches == 12
    assert settings.reasoning_model_timeout_seconds == 45.5
    assert settings.reasoning_session_deadline_seconds == 300.5


def test_unknown_m6_dotenv_entry_is_rejected(tmp_path: Path) -> None:
    dotenv = tmp_path / "m6.env"
    dotenv.write_text("WATERAGENTS_REASONING_UNKNOWN=1\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="reasoning_unknown"):
        AppSettings(_env_file=dotenv)


def test_router_uses_only_catalog_authorized_selected_adapter() -> None:
    selected_port = RecordingPort()
    other_port = RecordingPort()
    router = WaterbodyModelRouter(
        catalog(capability(OPENAI_MODEL, ModelRole.WATERBODY)),
        (registration("openai", selected_port), registration("other", other_port)),
    )

    events = run(_collect(router, request()))

    assert len(events) == 2
    assert selected_port.calls == [request()]
    assert other_port.calls == []
    assert router.model_fit(OPENAI_MODEL).model == OPENAI_MODEL


def test_router_uses_model_specific_limits_for_same_provider() -> None:
    small = ModelSelection(provider="openai", model="waterbody-small")
    large = ModelSelection(provider="openai", model="waterbody-large")
    router = WaterbodyModelRouter(
        catalog(
            capability(
                small,
                ModelRole.WATERBODY,
                context_window_tokens=8_192,
                max_output_tokens=2_048,
            ),
            capability(
                large,
                ModelRole.WATERBODY,
                context_window_tokens=128_000,
                max_output_tokens=32_768,
            ),
        ),
        (registration("openai"),),
    )

    small_fit = router.model_fit(small)
    large_fit = router.model_fit(large)

    assert (small_fit.context_window_tokens, small_fit.max_output_tokens) == (
        8_192,
        2_048,
    )
    assert (large_fit.context_window_tokens, large_fit.max_output_tokens) == (
        128_000,
        32_768,
    )


async def _collect(router: WaterbodyModelRouter, model_request):
    return [event async for event in router.stream(model_request)]


@pytest.mark.parametrize(
    ("selection", "capabilities", "registrations", "message"),
    [
        (
            ModelSelection(provider="unsupported", model="waterbody-v1"),
            (
                capability(
                    ModelSelection(provider="unsupported", model="waterbody-v1"),
                    ModelRole.WATERBODY,
                ),
            ),
            (registration("openai"),),
            "adapter is unavailable",
        ),
        (
            ModelSelection(provider="openai", model="unknown"),
            (capability(OPENAI_MODEL, ModelRole.WATERBODY),),
            (registration("openai"),),
            "not authorized",
        ),
        (
            OPENAI_MODEL,
            (capability(OPENAI_MODEL, ModelRole.QUERY_PARSER),),
            (registration("openai"),),
            "not authorized",
        ),
    ],
)
def test_router_fails_closed_without_fallback(
    selection,
    capabilities,
    registrations,
    message,
) -> None:
    router = WaterbodyModelRouter(catalog(*capabilities), registrations)

    with pytest.raises(SettingsConfigurationError, match=message) as captured:
        router.require(selection)

    assert "credential" not in str(captured.value).casefold()
    assert all(registration.port.calls == [] for registration in registrations)


def test_runtime_composes_the_real_chain_and_reuses_m5_m3_instances() -> None:
    settings = AppSettings(_env_file=None)
    arguments = composition_arguments()

    runtime = compose_m6_runtime(settings, **arguments)

    assert runtime.runner._operations is arguments["operations"]
    assert runtime.runner._m5_runtime is arguments["m5_runtime"]
    assert (
        runtime.runner._batch_planner._prompt_builder
        is runtime.runner._prompt_builder
    )
    assert runtime.safe_metadata() == {
        "limits": {
            "planning_turns": 6,
            "tool_calls_per_turn": 4,
            "cells_per_batch": 32,
            "batches": 24,
            "model_timeout_seconds": 90.0,
            "session_deadline_seconds": 600.0,
            "repairs_per_exchange": 1,
            "maximum_model_calls": 56,
        },
        "adapter_providers": ("openai",),
    }


def test_deadline_reached_rejects_before_transcript_or_model_operation() -> None:
    port = RecordingPort()
    runtime = compose_m6_runtime(
        AppSettings(_env_file=None),
        **composition_arguments(port),
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="has been reached"):
            await runtime.runner.run(object(), deadline=FIXED_NOW)

    run(exercise())
    assert port.calls == []


def test_test_builder_and_direct_production_composition_preserve_contract() -> None:
    settings = AppSettings(_env_file=None)
    production_port = RecordingPort()
    production_arguments = composition_arguments(production_port)
    production = compose_m6_runtime(settings, **production_arguments)
    test_port = RecordingPort()
    test_arguments = composition_arguments(test_port)

    async def exercise():
        async with build_m6_test_runtime(settings, **test_arguments) as test_runtime:
            production_events = await _collect(
                production.model_port_router,
                request(),
            )
            test_events = await _collect(test_runtime.model_port_router, request())
            return production.safe_metadata(), test_runtime.safe_metadata(), (
                tuple(event.model_dump(mode="json") for event in production_events),
                tuple(event.model_dump(mode="json") for event in test_events),
            )

    production_metadata, test_metadata, event_contracts = run(exercise())

    assert production_metadata == test_metadata
    assert event_contracts[0] == event_contracts[1]


def test_production_builder_routes_openai_without_issuing_a_request() -> None:
    settings = AppSettings(_env_file=None, openai_api_key="test-only-key")
    arguments = composition_arguments()
    arguments.pop("registrations")

    async def exercise():
        async with build_m6_runtime(settings, **arguments) as runtime:
            return runtime.model_port_router.providers, runtime.safe_metadata()

    providers, metadata = run(exercise())

    assert providers == ("openai",)
    assert metadata["adapter_providers"] == ("openai",)


@pytest.mark.parametrize(
    "capabilities",
    [
        (
            capability(
                ModelSelection(provider="unsupported", model="waterbody-v1"),
                ModelRole.WATERBODY,
            ),
        ),
        (
            capability(OPENAI_MODEL, ModelRole.WATERBODY),
            capability(
                ModelSelection(provider="unsupported", model="waterbody-v1"),
                ModelRole.WATERBODY,
            ),
        ),
    ],
)
def test_production_builder_rejects_unsupported_waterbody_providers(
    capabilities,
) -> None:
    settings = AppSettings(_env_file=None, openai_api_key="test-only-key")
    arguments = composition_arguments()
    arguments.pop("registrations")
    arguments["model_catalog"] = catalog(*capabilities)

    async def exercise() -> None:
        with pytest.raises(
            SettingsConfigurationError,
            match="unsupported waterbody model provider",
        ):
            async with build_m6_runtime(settings, **arguments):
                raise AssertionError("runtime started with an unsupported provider")

    run(exercise())


def test_production_builder_rejects_missing_openai_credential_without_leaking() -> None:
    settings = AppSettings(_env_file=None)
    arguments = composition_arguments()
    arguments.pop("registrations")

    async def exercise() -> None:
        with pytest.raises(SettingsConfigurationError) as captured:
            async with build_m6_runtime(settings, **arguments):
                raise AssertionError("runtime started without a credential")
        assert "openai_api_key is required" in str(captured.value)

    run(exercise())


def test_application_lifespan_exposes_internal_m6_with_single_shared_runtime_chain() -> None:
    application = create_test_app(
        AppSettings(_env_file=None),
        transcript_store=InMemoryTranscriptStore(),
        shutdown_grace_period=0,
    )

    async def exercise() -> None:
        async with application.router.lifespan_context(application):
            m6 = application.state.m6_runtime
            assert m6.runner._operations is application.state.run_coordinator
            assert m6.runner._m5_runtime.executor is application.state.m5_runtime.executor

    run(exercise())


def test_m6_context_closes_after_coordinator_shutdown() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def tracked_builder(settings, **kwargs):
        async with _build_test_m6_runtime(settings, **kwargs) as runtime:
            try:
                yield runtime
            finally:
                events.append("m6_closed")

    application = create_test_app(
        AppSettings(_env_file=None),
        transcript_store=InMemoryTranscriptStore(),
        shutdown_grace_period=0,
        m6_runtime_builder=tracked_builder,
    )

    async def exercise() -> None:
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            shutdown = coordinator.shutdown

            async def tracked_shutdown():
                events.append("coordinator_shutdown")
                await shutdown()

            coordinator.shutdown = tracked_shutdown

    run(exercise())

    assert events == ["coordinator_shutdown", "m6_closed"]


def test_openapi_keeps_exact_existing_surface_and_safe_m6_metadata() -> None:
    settings = AppSettings(
        _env_file=None,
        openai_api_key="metadata-secret",
        model_catalog=(capability(OPENAI_MODEL, ModelRole.WATERBODY),),
    )
    application = create_test_app(settings, model_catalog=MODEL_CATALOG)
    schema = application.openapi()
    paths = set(schema["paths"])

    assert len(paths) == 14
    assert "/api/settings" in paths
    assert "/api/keys" in paths
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in (
            "/reasoning",
            "/models/complete",
            "/tools",
            "/evidence/collect",
            "/__test__",
        )
    )
    encoded = json.dumps(schema["x-m6-runtime"], sort_keys=True)
    assert schema["x-m6-runtime"]["limits"]["maximum_model_calls"] == 56
    assert "metadata-secret" not in encoded
    assert settings.openai_base_url not in encoded
    assert "prompt" not in encoded.casefold()


def test_safe_metadata_reports_wired_adapter_independent_of_mutable_key() -> None:
    model_catalog = catalog(capability(OPENAI_MODEL, ModelRole.WATERBODY))

    assert safe_m6_metadata(
        AppSettings(_env_file=None),
        model_catalog,
    )["adapter_providers"] == ("openai",)
    assert safe_m6_metadata(
        AppSettings(_env_file=None, openai_api_key="configured"),
        model_catalog,
    )["adapter_providers"] == ("openai",)


def test_size_estimator_uses_exact_bytes_and_response_cells() -> None:
    estimator = OpenAIResponsesSizeEstimator()
    estimate = estimator.estimate(
        OPENAI_MODEL,
        serialized_request=b"x" * 10,
        serialized_response_shape=json.dumps(
            {"cell_ids": ["cell-1", "cell-2"]},
            separators=(",", ":"),
        ).encode(),
    )

    assert estimate.input_tokens == 4
    assert estimate.output_tokens > 512
    assert estimate.estimated_output_bytes > 2048


def test_provider_smoke_is_opt_in_and_secret_free_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(m6_provider_smoke.OPT_IN, raising=False)
    monkeypatch.setenv("WATERAGENTS_OPENAI_API_KEY", "must-not-appear")

    result = run(m6_provider_smoke._main())
    encoded = json.dumps(result, sort_keys=True)

    assert result["status"] == "not_run"
    assert result["requests"] == 0
    assert "must-not-appear" not in encoded
    assert "prompt" not in encoded.casefold()
