"""Test-only M6 composition that swaps only external model capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from app.application.analysis_runs.workflow import WorkflowOperations
from app.domain import ModelCatalogSnapshot
from app.domain.hydrology import ParameterCapabilityRegistry
from app.infrastructure.config import AppSettings
from app.infrastructure.runtime.m5 import M5Runtime
from app.infrastructure.runtime.m6 import (
    M6Runtime,
    WaterbodyAdapterRegistration,
    compose_m6_runtime,
)
from app.ports.clock import ClockPort


@asynccontextmanager
async def build_m6_test_runtime(
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
) -> AsyncIterator[M6Runtime]:
    """Build the same production chain with explicit deterministic test ports."""

    yield compose_m6_runtime(
        settings,
        operations=operations,
        m5_runtime=m5_runtime,
        capability_registry=capability_registry,
        model_catalog=model_catalog,
        registrations=registrations,
        clock=clock,
        operation_id_factory=operation_id_factory,
        result_id_factory=result_id_factory,
        error_id_factory=error_id_factory,
        private_values=private_values,
    )
