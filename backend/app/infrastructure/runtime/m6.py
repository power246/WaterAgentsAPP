from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from uuid import UUID, uuid4

from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.evidence_tools import EvidenceAcquisitionHandoff
from app.application.reasoning import (
    EvidencePlanningLoop,
    ModelFitCapability,
    ModelSizeEstimate,
    ModelSizeEstimator,
    PlanningLoopLimits,
    ReasoningBatchPlanner,
    ReasoningPlannerLimits,
    RunSingleWaterbodyAgent,
    ValidatedWaterbodyAgentTask,
    WaterbodyAgentRunOutcome,
)
from app.application.reasoning.model_models import (
    WaterbodyModelEvent,
    WaterbodyModelRequest,
)
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.application.reasoning.transcript_store import ReasoningTranscriptStore
from app.domain import ModelCatalogSnapshot, ModelRole, ModelSelection
from app.domain.hydrology import ParameterCapabilityRegistry
from app.infrastructure.adapters import OpenAIResponsesWaterbodyAdapter
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.runtime.m5 import M5Runtime
from app.ports.clock import ClockPort
from app.ports.waterbody_model import WaterbodyModelPort

_ESTIMATED_OUTPUT_TOKENS_PER_CELL = 256
_ESTIMATED_OUTPUT_BYTES_PER_CELL = 1024


@dataclass(frozen=True, slots=True)
class M6RuntimeLimits:
    planning_turns: int
    tool_calls_per_turn: int
    cells_per_batch: int
    batches: int
    model_timeout_seconds: float
    session_deadline_seconds: float
    repairs_per_exchange: int = 1

    @property
    def maximum_model_calls(self) -> int:
        return self.planning_turns + 2 * self.batches + 2


@dataclass(frozen=True, slots=True)
class WaterbodyAdapterRegistration:
    provider: str
    port: WaterbodyModelPort
    size_estimator: ModelSizeEstimator

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("waterbody adapter provider must not be empty")
        if not callable(getattr(self.size_estimator, "estimate", None)):
            raise ValueError("waterbody adapter requires a size estimator")


class OpenAIResponsesSizeEstimator:
    """Conservative deterministic sizing over the exact canonical byte inputs."""

    def estimate(
        self,
        model: ModelSelection,
        *,
        serialized_request: bytes,
        serialized_response_shape: bytes,
    ) -> ModelSizeEstimate:
        del model
        try:
            shape = json.loads(serialized_response_shape)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("model response shape is invalid") from error
        if not isinstance(shape, dict):
            raise ValueError("model response shape is invalid")
        if "cell_ids" in shape:
            cell_ids = shape["cell_ids"]
            if not isinstance(cell_ids, list) or not all(
                isinstance(item, str) and item for item in cell_ids
            ):
                raise ValueError("model response cell identities are invalid")
            output_units = len(cell_ids)
        else:
            highlight_limit = shape.get("summary_highlight_limit")
            if type(highlight_limit) is not int or not 0 < highlight_limit <= 32:
                raise ValueError("model summary response shape is invalid")
            output_units = highlight_limit
        return ModelSizeEstimate(
            input_tokens=ceil(len(serialized_request) / 3),
            output_tokens=(
                ceil(len(serialized_response_shape) / 3)
                + output_units * _ESTIMATED_OUTPUT_TOKENS_PER_CELL
            ),
            estimated_output_bytes=(
                len(serialized_response_shape)
                + output_units * _ESTIMATED_OUTPUT_BYTES_PER_CELL
            ),
        )


class WaterbodyModelRouter:
    """Route only catalog-authorized waterbody models to configured adapters."""

    def __init__(
        self,
        catalog: ModelCatalogSnapshot,
        registrations: tuple[WaterbodyAdapterRegistration, ...],
    ) -> None:
        self._catalog = catalog
        by_provider: dict[str, WaterbodyAdapterRegistration] = {}
        for registration in registrations:
            provider = registration.provider.casefold()
            if provider in by_provider:
                raise SettingsConfigurationError(
                    f"duplicate waterbody adapter provider: {registration.provider}"
                )
            by_provider[provider] = registration
        self._registrations = by_provider

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def require(self, selection: ModelSelection) -> WaterbodyAdapterRegistration:
        if not self._catalog.supports(selection, ModelRole.WATERBODY):
            raise SettingsConfigurationError(
                "waterbody model is not authorized for the waterbody role "
                f"(provider={selection.provider}, model={selection.model})"
            )
        registration = self._registrations.get(selection.provider.casefold())
        if registration is None:
            raise SettingsConfigurationError(
                "waterbody adapter is unavailable "
                f"(provider={selection.provider}, model={selection.model})"
            )
        return registration

    def model_fit(self, selection: ModelSelection) -> ModelFitCapability:
        self.require(selection)
        capability = self._catalog.capability_for(selection, ModelRole.WATERBODY)
        if capability is None:
            raise SettingsConfigurationError(
                "waterbody model is not authorized for the waterbody role "
                f"(provider={selection.provider}, model={selection.model})"
            )
        if (
            capability.context_window_tokens is None
            or capability.max_output_tokens is None
        ):
            raise SettingsConfigurationError(
                "waterbody model token limits are unavailable "
                f"(provider={selection.provider}, model={selection.model})"
            )
        return ModelFitCapability(
            model=selection,
            context_window_tokens=capability.context_window_tokens,
            max_output_tokens=capability.max_output_tokens,
        )

    def estimate(
        self,
        model: ModelSelection,
        *,
        serialized_request: bytes,
        serialized_response_shape: bytes,
    ) -> ModelSizeEstimate:
        registration = self.require(model)
        return registration.size_estimator.estimate(
            model,
            serialized_request=serialized_request,
            serialized_response_shape=serialized_response_shape,
        )

    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        registration = self.require(request.model)
        async for event in registration.port.stream(request):
            yield event


class M6RunnerService:
    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        model_router: WaterbodyModelRouter,
        m5_runtime: M5Runtime,
        capability_registry: ParameterCapabilityRegistry,
        prompt_builder: PromptBuilder,
        batch_planner: ReasoningBatchPlanner,
        limits: M6RuntimeLimits,
        clock: ClockPort,
        operation_id_factory: Callable[[], UUID],
        result_id_factory: Callable[[], str],
        error_id_factory: Callable[[], UUID],
        private_values: tuple[str, ...],
    ) -> None:
        self._operations = operations
        self._model_router = model_router
        self._m5_runtime = m5_runtime
        self._capability_registry = capability_registry
        self._prompt_builder = prompt_builder
        self._batch_planner = batch_planner
        self._limits = limits
        self._clock = clock
        self._operation_id_factory = operation_id_factory
        self._result_id_factory = result_id_factory
        self._error_id_factory = error_id_factory
        self._private_values = private_values

    def session_deadline(self) -> datetime:
        return self._clock.now() + timedelta(
            seconds=self._limits.session_deadline_seconds
        )

    def build_runner(
        self,
        task: ValidatedWaterbodyAgentTask,
    ) -> RunSingleWaterbodyAgent:
        model_fit = self._model_router.model_fit(task.task.waterbody_model)
        store = ReasoningTranscriptStore(
            operations=self._operations,
            run_id=task.task.run_id,
            session_id=task.task.session_id,
            private_values=self._private_values,
        )
        recording_port = store.recording_port(self._model_router)
        planning_loop = EvidencePlanningLoop(
            acquisition=self._m5_runtime.acquisition,
            capability_registry=self._capability_registry,
            prompt_builder=self._prompt_builder,
            model_port=recording_port,
            limits=PlanningLoopLimits(
                max_turns=self._limits.planning_turns,
                max_tool_calls_per_turn=self._limits.tool_calls_per_turn,
            ),
            clock=self._clock,
            operation_id_factory=self._operation_id_factory,
            transcript_store=store,
        )
        return RunSingleWaterbodyAgent(
            planning_loop=planning_loop,
            capability_registry=self._capability_registry,
            batch_planner=self._batch_planner,
            prompt_builder=self._prompt_builder,
            model_port=recording_port,
            model_fit=model_fit,
            result_id_factory=self._result_id_factory,
            error_id_factory=self._error_id_factory,
            clock=self._clock,
            transcript_store=store,
        )

    async def run(
        self,
        task: ValidatedWaterbodyAgentTask,
        *,
        deadline: datetime,
        initial_handoff: EvidenceAcquisitionHandoff | None = None,
    ) -> WaterbodyAgentRunOutcome:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("waterbody reasoning deadline must be timezone-aware")
        now = self._clock.now()
        if now >= deadline:
            raise ValueError("waterbody reasoning deadline has been reached")
        if deadline > now + timedelta(seconds=self._limits.session_deadline_seconds):
            raise ValueError("waterbody reasoning deadline exceeds the runtime limit")
        return await self.build_runner(task).run(
            task,
            deadline=deadline,
            initial_handoff=initial_handoff,
        )


@dataclass(frozen=True, slots=True)
class M6Runtime:
    runner: M6RunnerService
    model_port_router: WaterbodyModelRouter
    limits: M6RuntimeLimits
    private_values: tuple[str, ...] = ()

    def safe_metadata(self) -> dict[str, object]:
        return _build_safe_m6_metadata(
            self.limits,
            self.model_port_router.providers,
        )


def m6_runtime_limits(settings: AppSettings) -> M6RuntimeLimits:
    return M6RuntimeLimits(
        planning_turns=settings.reasoning_max_planning_turns,
        tool_calls_per_turn=settings.reasoning_max_tool_calls_per_turn,
        cells_per_batch=settings.reasoning_max_cells_per_batch,
        batches=settings.reasoning_max_batches,
        model_timeout_seconds=settings.reasoning_model_timeout_seconds,
        session_deadline_seconds=settings.reasoning_session_deadline_seconds,
    )


def compose_m6_runtime(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    m5_runtime: M5Runtime,
    capability_registry: ParameterCapabilityRegistry,
    model_catalog: ModelCatalogSnapshot,
    registrations: tuple[WaterbodyAdapterRegistration, ...],
    clock: ClockPort,
    operation_id_factory: Callable[[], UUID] = uuid4,
    result_id_factory: Callable[[], str] | None = None,
    error_id_factory: Callable[[], UUID] = uuid4,
    private_values: tuple[str, ...] = (),
) -> M6Runtime:
    limits = m6_runtime_limits(settings)
    router = WaterbodyModelRouter(model_catalog, registrations)
    prompt_builder = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY)
    batch_planner = ReasoningBatchPlanner(
        prompt_builder=prompt_builder,
        model_catalog=model_catalog,
        size_estimator=router,
        limits=ReasoningPlannerLimits(
            max_cells_per_batch=limits.cells_per_batch,
            max_batches=limits.batches,
        ),
    )
    resolved_result_id_factory = result_id_factory or (
        lambda: str(operation_id_factory())
    )
    runner = M6RunnerService(
        operations=operations,
        model_router=router,
        m5_runtime=m5_runtime,
        capability_registry=capability_registry,
        prompt_builder=prompt_builder,
        batch_planner=batch_planner,
        limits=limits,
        clock=clock,
        operation_id_factory=operation_id_factory,
        result_id_factory=resolved_result_id_factory,
        error_id_factory=error_id_factory,
        private_values=private_values,
    )
    return M6Runtime(
        runner=runner,
        model_port_router=router,
        limits=limits,
        private_values=private_values,
    )


@asynccontextmanager
async def build_m6_runtime(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    m5_runtime: M5Runtime,
    capability_registry: ParameterCapabilityRegistry,
    model_catalog: ModelCatalogSnapshot,
    clock: ClockPort,
    operation_id_factory: Callable[[], UUID] = uuid4,
    result_id_factory: Callable[[], str] | None = None,
    error_id_factory: Callable[[], UUID] = uuid4,
) -> AsyncIterator[M6Runtime]:
    providers = {
        capability.provider.casefold()
        for capability in model_catalog.capabilities
        if ModelRole.WATERBODY in capability.roles
    }
    if not providers:
        raise SettingsConfigurationError(
            "model_catalog must configure at least one waterbody model"
        )
    unsupported = providers - {"openai"}
    if unsupported:
        raise SettingsConfigurationError(
            "unsupported waterbody model provider: "
            + ", ".join(sorted(unsupported))
        )
    if settings.openai_api_key is None:
        raise SettingsConfigurationError("openai_api_key is required")
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise SettingsConfigurationError("openai_api_key is required")

    async with ProviderHttpClient(
        timeout_seconds=settings.reasoning_model_timeout_seconds,
        max_response_bytes=settings.provider_http_max_response_bytes,
        retry_delay_seconds=settings.provider_http_retry_delay_seconds,
        attempts=1,
    ) as http:
        adapter = OpenAIResponsesWaterbodyAdapter(
            http,
            api_key=api_key,
            base_url=settings.openai_base_url,
        )
        yield compose_m6_runtime(
            settings,
            operations=operations,
            m5_runtime=m5_runtime,
            capability_registry=capability_registry,
            model_catalog=model_catalog,
            registrations=(
                WaterbodyAdapterRegistration(
                    provider="openai",
                    port=adapter,
                    size_estimator=OpenAIResponsesSizeEstimator(),
                ),
            ),
            clock=clock,
            operation_id_factory=operation_id_factory,
            result_id_factory=result_id_factory,
            error_id_factory=error_id_factory,
            private_values=(api_key,),
        )


def safe_m6_metadata(
    settings: AppSettings,
    model_catalog: ModelCatalogSnapshot,
) -> dict[str, object]:
    limits = m6_runtime_limits(settings)
    openai_available = (
        settings.openai_api_key is not None
        and bool(settings.openai_api_key.get_secret_value())
    )
    providers = tuple(
        sorted(
            {
                capability.provider.casefold()
                for capability in model_catalog.capabilities
                if ModelRole.WATERBODY in capability.roles
                and capability.provider.casefold() == "openai"
                and openai_available
            }
        )
    )
    return _build_safe_m6_metadata(limits, providers)


def _build_safe_m6_metadata(
    limits: M6RuntimeLimits,
    providers: tuple[str, ...],
) -> dict[str, object]:
    return {
        "limits": {
            "planning_turns": limits.planning_turns,
            "tool_calls_per_turn": limits.tool_calls_per_turn,
            "cells_per_batch": limits.cells_per_batch,
            "batches": limits.batches,
            "model_timeout_seconds": limits.model_timeout_seconds,
            "session_deadline_seconds": limits.session_deadline_seconds,
            "repairs_per_exchange": limits.repairs_per_exchange,
            "maximum_model_calls": limits.maximum_model_calls,
        },
        "adapter_providers": providers,
    }
