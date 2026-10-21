from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.transcript_memory import (
    GenerationConfigurationError,
    GenerationService,
    GenerationStartupState,
    RAGBackfillCoordinator,
    RAGDiscoveryService,
    RAGIndexWorker,
    RAGMaintenanceOperations,
    RAGReconciliationRunner,
    RAGReconciliationSignal,
    RAGSearchService,
    TranscriptEvidenceProducer,
    TranscriptRAGCaseSourceReader,
    assess_generation_startup,
    validate_desired_embedding_profile,
)
from app.application.transcripts.registry import TranscriptRegistry
from app.domain import (
    EmbeddingProfile,
    ModelCatalogSnapshot,
    RAGIndexGenerationStatus,
)
from app.application.settings_admin.credential_service import CredentialService
from app.infrastructure.adapters import OpenAIEmbeddingAdapter
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.credentials.embedding import embedding_api_key, embedding_base_url, lease_embedding_api_key
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.observability.rag_metrics import RAGRuntimeMetrics
from app.infrastructure.persistence import (
    PostgresRAGRepository,
    RAGPersistenceConflictError,
)
from app.ports import (
    ClockPort,
    EmbeddingCancelledError,
    EmbeddingPort,
    PortAuthenticationError,
    InvalidPortResponseError,
    PortConfigurationError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
)


class _ObservedEmbeddingPort:
    def __init__(
        self,
        port: EmbeddingPort,
        metrics: RAGRuntimeMetrics,
        *,
        stage: str,
        monotonic: Callable[[], float],
    ) -> None:
        self._port = port
        self._metrics = metrics
        self._stage = stage
        self._monotonic = monotonic

    async def embed(self, profile, texts, deadline, cancel_token):
        started = self._monotonic()
        try:
            result = await self._port.embed(
                profile,
                texts,
                deadline,
                cancel_token,
            )
        except BaseException as error:
            self._metrics.observe_embedding(
                stage=self._stage,
                outcome=_embedding_outcome(error),
                latency_seconds=self._monotonic() - started,
            )
            raise
        self._metrics.observe_embedding(
            stage=self._stage,
            outcome="completed",
            latency_seconds=self._monotonic() - started,
        )
        return result


class AttemptEmbeddingFactory:
    """Read one effective lease for each claimed embedding batch attempt."""

    def __init__(self, *, credentials: CredentialService,
                 http: ProviderHttpClient, settings: AppSettings,
                 metrics: RAGRuntimeMetrics, monotonic: Callable[[], float]) -> None:
        self._credentials = credentials
        self._http = http
        self._settings = settings
        self._metrics = metrics
        self._monotonic = monotonic

    async def __call__(self) -> EmbeddingPort:
        key = await lease_embedding_api_key(self._settings, self._credentials)
        return _ObservedEmbeddingPort(
            OpenAIEmbeddingAdapter(
                self._http, api_key=key.get_secret_value(),
                base_url=embedding_base_url(self._settings),
            ),
            self._metrics, stage="indexing", monotonic=self._monotonic,
        )


class RunEmbeddingRegistry:
    def __init__(self) -> None:
        self._ports: dict[str, EmbeddingPort] = {}

    def bind(self, run_id: str, port: EmbeddingPort) -> None:
        if run_id in self._ports:
            raise ValueError("run embedding is already bound")
        self._ports[run_id] = port

    def get(self, run_id: str) -> EmbeddingPort | None:
        return self._ports.get(run_id)

    def release(self, run_id: str) -> None:
        self._ports.pop(run_id, None)


class _ObservedSearchIndex:
    def __init__(
        self,
        repository: PostgresRAGRepository,
        metrics: RAGRuntimeMetrics,
    ) -> None:
        self._repository = repository
        self._metrics = metrics

    async def search_candidates(self, query):
        candidates = await self._repository.search_candidates(query)
        self._metrics.observe_search_candidates(
            strategy=query.intent.strategy.value,
            candidate_count=len(candidates),
        )
        return candidates


@dataclass(slots=True)
class M8Runtime:
    producer: TranscriptEvidenceProducer
    worker: RAGIndexWorker
    backfill: RAGBackfillCoordinator
    discovery: RAGDiscoveryService
    repository: PostgresRAGRepository
    operations: RAGMaintenanceOperations
    signal: RAGReconciliationSignal
    runner: RAGReconciliationRunner
    metrics: RAGRuntimeMetrics
    startup_state: GenerationStartupState
    desired_profile: EmbeddingProfile
    clock: ClockPort
    shutdown_grace_seconds: float
    memory_enabled: bool = True
    run_embeddings: RunEmbeddingRegistry | None = None
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _stopping: bool = field(default=False, init=False, repr=False)

    def bind_run_embedding(self, run_id: str, port: EmbeddingPort) -> None:
        if self.run_embeddings is None:
            raise RuntimeError("run embedding registry is unavailable")
        self.run_embeddings.bind(
            run_id,
            _ObservedEmbeddingPort(
                port, self.metrics, stage="search",
                monotonic=asyncio.get_running_loop().time,
            ),
        )

    def release_run_embedding(self, run_id: str) -> None:
        if self.run_embeddings is not None:
            self.run_embeddings.release(run_id)

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("M8 runtime is already started")
        await self.observe_health()
        self._started = True
        if not self.memory_enabled:
            return
        self._task = asyncio.create_task(
            self.runner.run(self._reconcile_guarded),
            name="rag-index-worker",
        )

    def notify_run_terminal(self, _run_id: object) -> None:
        if self.memory_enabled and not self._stopping:
            self.signal.notify_run_terminal()

    def notify_admin_maintenance(self) -> None:
        if self.memory_enabled and not self._stopping:
            self.signal.notify_run_terminal()

    async def apply_profile_handoff(self, profile: EmbeddingProfile) -> str:
        """Use the existing M8 worker and activation gates for a desired profile."""
        if not self.memory_enabled:
            raise RuntimeError("historical memory is disabled")
        if self._stopping:
            raise RuntimeError("RAG runtime is shutting down")
        await self.operations.build_generation(profile)
        self.desired_profile = profile
        self.notify_admin_maintenance()
        async with asyncio.timeout(600):
            while True:
                active = await self.repository.read_active_generation()
                if active is not None and active.profile == profile:
                    return active.generation_id
                generations = await self.repository.list_generations()
                if any(
                    item.profile == profile and item.status is RAGIndexGenerationStatus.FAILED
                    for item in generations
                ):
                    raise RuntimeError("RAG generation build failed")
                await asyncio.sleep(0.5)

    async def shutdown(self) -> None:
        if self._stopping:
            task = self._task
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            return
        self._stopping = True
        self.runner.request_stop()
        self.worker.request_stop()
        await self.worker.drain(self.shutdown_grace_seconds)
        task = self._task
        if task is None:
            return
        try:
            async with asyncio.timeout(self.shutdown_grace_seconds):
                await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _reconcile_guarded(self) -> None:
        try:
            await self._reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.metrics.observe_reconciliation_failure(
                _reconciliation_error_code(error)
            )

    async def _reconcile(self) -> None:
        if self._stopping:
            return
        await self.backfill.ensure_building_generation(self.desired_profile)
        result = await self.backfill.reconcile(discovered_at=self.clock.now())
        generations = await self.repository.list_generations()
        for generation in generations:
            if self._stopping:
                return
            if generation.status in {
                RAGIndexGenerationStatus.ACTIVE,
                RAGIndexGenerationStatus.BUILDING,
            }:
                await self.worker.run_available(generation)

        if self._stopping:
            return

        building = next(
            (
                generation
                for generation in await self.repository.list_generations()
                if generation.status is RAGIndexGenerationStatus.BUILDING
            ),
            None,
        )
        building_discovery = result.building
        if building is not None and building_discovery is not None:
            counts = await self.repository.counts(building.generation_id)
            if (
                building_discovery.exhausted
                and counts.pending == 0
                and counts.leased == 0
                and counts.retry_wait == 0
                and counts.failed == 0
            ):
                if self._stopping:
                    return
                await self.backfill.activate_when_complete(
                    building,
                    activated_at=self.clock.now(),
                )
        await self.observe_health()

    async def observe_health(self) -> None:
        now = self.clock.now()
        generations = await self.repository.list_generations()
        active = next(
            (
                generation
                for generation in generations
                if generation.status is RAGIndexGenerationStatus.ACTIVE
            ),
            None,
        )
        active_digest = None if active is None else active.profile.profile_digest
        for generation in generations:
            counts = await self.repository.counts(generation.generation_id)
            oldest = await self.repository.oldest_incomplete_job_at(
                generation.generation_id
            )
            self.metrics.observe_generation(
                status=generation.status.value,
                age_seconds=(now - generation.created_at).total_seconds(),
                counts=counts,
                lag_seconds=(
                    0 if oldest is None else (now - oldest).total_seconds()
                ),
                active_profile_digest=active_digest,
            )


@asynccontextmanager
async def build_m8_runtime(
    settings: AppSettings,
    *,
    engine: AsyncEngine,
    transcript_reader: object,
    transcript_registry: TranscriptRegistry,
    model_catalog: ModelCatalogSnapshot,
    clock: ClockPort,
    foundation_validated: bool,
    embedding_port: EmbeddingPort | None = None,
    lease_owner_factory: Callable[[], str] | None = None,
    credential_service: CredentialService | None = None,
    provider_http: ProviderHttpClient | None = None,
) -> AsyncIterator[M8Runtime]:
    try:
        desired_profile = validate_desired_embedding_profile(model_catalog)
    except GenerationConfigurationError as error:
        raise SettingsConfigurationError(str(error)) from error
    if not foundation_validated:
        raise SettingsConfigurationError(
            "RAG database schema or vector foundation is not validated"
        )
    if credential_service is not None and provider_http is None:
        raise SettingsConfigurationError("dynamic embedding requires a shared HTTP pool")
    if credential_service is not None and desired_profile.provider.casefold() != "openai":
        raise SettingsConfigurationError("unsupported embedding provider configuration")

    async with AsyncExitStack() as stack:
        dynamic = credential_service is not None
        if dynamic and embedding_port is not None:
            raise SettingsConfigurationError("dynamic embedding cannot use a fixed adapter")
        active_embedding = embedding_port
        if not dynamic and active_embedding is None:
            if desired_profile.provider.casefold() != "openai":
                raise SettingsConfigurationError(
                    "unsupported embedding provider configuration: "
                    + desired_profile.provider
                )
            try:
                api_key = embedding_api_key(settings).get_secret_value()
            except PortAuthenticationError:
                raise SettingsConfigurationError("embedding credential is required") from None
            http = await stack.enter_async_context(
                ProviderHttpClient(
                    timeout_seconds=settings.rag_embedding_timeout_seconds,
                    max_response_bytes=settings.provider_http_max_response_bytes,
                    retry_delay_seconds=0,
                    attempts=1,
                )
            )
            active_embedding = OpenAIEmbeddingAdapter(
                http,
                api_key=api_key,
                base_url=embedding_base_url(settings),
            )

        loop = asyncio.get_running_loop()
        metrics = RAGRuntimeMetrics()
        repository = PostgresRAGRepository(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        discovery = RAGDiscoveryService(
            repository,
            batch_size=settings.rag_discovery_batch_size,
        )
        backfill = RAGBackfillCoordinator(
            repository,
            discovery,
            max_discovery_batches=settings.rag_reconciliation_max_batches,
        )
        source_reader = TranscriptRAGCaseSourceReader(
            transcript_reader,
            transcript_registry,
        )
        try:
            initial_generations = await repository.list_generations()
            initial_dimensions = await repository.active_projection_dimensions()
            if initial_generations:
                # A saved Settings desire needs an explicit Admin apply. Resume a
                # durable building generation, otherwise retain the visible active
                # profile across process restarts.
                persisted_target = next(
                    (item for item in initial_generations
                     if item.status is RAGIndexGenerationStatus.BUILDING), None,
                ) or next(
                    (item for item in initial_generations
                     if item.status is RAGIndexGenerationStatus.ACTIVE), None,
                )
                if persisted_target is not None:
                    desired_profile = persisted_target.profile
                assess_generation_startup(
                    foundation_validated=foundation_validated,
                    desired_profile=desired_profile,
                    generations=initial_generations,
                    active_projection_dimensions=initial_dimensions,
                )
            elif initial_dimensions:
                raise GenerationConfigurationError(
                    "projection dimensions exist without a generation"
                )
            await backfill.ensure_building_generation(desired_profile)
            startup_state = assess_generation_startup(
                foundation_validated=foundation_validated,
                desired_profile=desired_profile,
                generations=await repository.list_generations(),
                active_projection_dimensions=(
                    await repository.active_projection_dimensions()
                ),
            )
        except (GenerationConfigurationError, ValueError) as error:
            raise SettingsConfigurationError(
                "M8 generation startup validation failed"
            ) from error

        run_embeddings = RunEmbeddingRegistry() if dynamic else None
        search_embedding = (
            None if dynamic else _ObservedEmbeddingPort(
                active_embedding, metrics, stage="search", monotonic=loop.time,
            )
        )
        worker_embedding = (
            None if dynamic else _ObservedEmbeddingPort(
                active_embedding, metrics, stage="indexing", monotonic=loop.time,
            )
        )

        attempt_embedding = (
            AttemptEmbeddingFactory(
                credentials=credential_service, http=provider_http,
                settings=settings, metrics=metrics, monotonic=loop.time,
            ) if dynamic else None
        )
        generations = GenerationService(repository)
        operations = RAGMaintenanceOperations(
            store=repository,
            backfill=backfill,
            generations=generations,
            clock=clock,
            retired_generation_retention_seconds=(
                settings.rag_retired_generation_retention_seconds
            ),
        )
        search = RAGSearchService(
            generations=generations,
            index=_ObservedSearchIndex(repository, metrics),
            source_loader=source_reader,
            embedding=search_embedding,
            embedding_for_run=run_embeddings.get if run_embeddings is not None else None,
            preview_max_bytes=settings.evidence_preview_max_bytes,
        )
        producer = TranscriptEvidenceProducer(
            search,
            observer=metrics,
            monotonic=loop.time,
        )
        worker = RAGIndexWorker(
            store=repository,
            source_loader=source_reader,
            embedding=worker_embedding,
            embedding_factory=attempt_embedding,
            clock=clock,
            lease_owner=(lease_owner_factory or (lambda: f"m8-{uuid4()}"))(),
            claim_batch_size=settings.rag_worker_claim_batch_size,
            embedding_batch_size=settings.rag_embedding_batch_size,
            max_concurrency=settings.rag_worker_max_concurrency,
            lease_seconds=settings.rag_job_lease_seconds,
            attempt_timeout_seconds=settings.rag_embedding_timeout_seconds,
            backoff_base_seconds=settings.rag_job_backoff_base_seconds,
            backoff_max_seconds=settings.rag_job_backoff_max_seconds,
            monotonic=loop.time,
        )
        signal = RAGReconciliationSignal()
        runtime = M8Runtime(
            producer=producer,
            worker=worker,
            backfill=backfill,
            discovery=discovery,
            repository=repository,
            operations=operations,
            signal=signal,
            runner=RAGReconciliationRunner(
                signal,
                interval_seconds=settings.rag_reconciliation_interval_seconds,
            ),
            metrics=metrics,
            startup_state=startup_state,
            desired_profile=desired_profile,
            clock=clock,
            shutdown_grace_seconds=settings.rag_worker_shutdown_grace_seconds,
            memory_enabled=settings.memory_enabled,
            run_embeddings=run_embeddings,
        )
        yield runtime


def _embedding_outcome(error: BaseException) -> str:
    if isinstance(error, EmbeddingCancelledError):
        return "cancelled"
    if isinstance(error, (TimeoutError, PortTimeoutError)):
        return "timeout"
    if isinstance(error, PortRateLimitError):
        return "rate_limited"
    if isinstance(error, PortUnavailableError):
        return "unavailable"
    if isinstance(
        error,
        (
            InvalidPortResponseError,
            PortConfigurationError,
            PortResponseLimitError,
            ValueError,
        ),
    ):
        return "invalid"
    return "failed"


def _reconciliation_error_code(error: BaseException) -> str:
    if isinstance(error, RAGPersistenceConflictError):
        return "persistence_conflict"
    if isinstance(error, PortUnavailableError):
        return "dependency_unavailable"
    if isinstance(error, (GenerationConfigurationError, ValueError)):
        return "configuration_invalid"
    return "reconciliation_failed"
