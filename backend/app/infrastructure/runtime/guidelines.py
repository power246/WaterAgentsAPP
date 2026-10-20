import asyncio
from math import ceil

from app.application.guidelines.embedding import desired_guideline_profile, guideline_embedding_failure
from app.application.guidelines.worker import GuidelinePreparationWorker
from app.application.guidelines.bundled import BundledGuidelineImporter, BundledImportReport
from app.application.embedding_validation import EmbeddingContractError
from app.application.settings_admin.credential_service import CredentialService
from app.application.settings_admin.errors import CredentialMissingError, CredentialStoreUnavailableError
from app.domain.model_catalog_snapshot import ModelCatalogSnapshot
from app.domain.settings_admin import CredentialLease, CredentialName
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.infrastructure.adapters.openai_guideline_embeddings import OpenAIGuidelineEmbeddingAdapter
from app.infrastructure.config import AppSettings
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from app.ports import PortAuthenticationError, PortConfigurationError
from app.ports.guidelines import BundledGuidelineSourcePort, GuidelineEmbeddingPort, GuidelineRepositoryPort


class GuidelineRuntime:
    """Guideline embedding lifetimes are independent of historical memory."""

    def __init__(self, *, settings: AppSettings, catalog: ModelCatalogSnapshot,
                 credentials: CredentialService, http: ProviderHttpClient | None) -> None:
        self.policy = settings.guideline_policy
        self._credentials = credentials
        self._http = http
        self._base_url = settings.openai_base_url
        self._run_embeddings: dict[str, GuidelineEmbeddingPort | None] = {}
        self.worker: GuidelinePreparationWorker | None = None
        self.import_report = BundledImportReport()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._started = False
        self._lease_seconds = max(60, ceil(self.policy.worker_timeout_seconds + 2 * settings.db_operation_timeout_seconds + 5))
        self._shutdown_grace = settings.shutdown_save_grace_seconds
        self.desired_profile = None
        self.safe_error_code = None
        try:
            profile = desired_guideline_profile(catalog, self.policy)
            if profile.provider.casefold() != "openai" or http is None:
                raise PortConfigurationError("Guideline embedding configuration is unavailable.")
            self.desired_profile = profile
        except (EmbeddingContractError, PortConfigurationError) as error:
            self.safe_error_code = guideline_embedding_failure(error)[0]

    def _adapter(self, lease: CredentialLease, timeout: float) -> GuidelineEmbeddingPort:
        if self.desired_profile is None or self._http is None or lease.name is not CredentialName.OPENAI:
            raise PortConfigurationError("Guideline embedding configuration is unavailable.", operation="embed")
        return OpenAIGuidelineEmbeddingAdapter(
            self._http, api_key=lease.value.get_secret_value(), base_url=self._base_url, timeout_seconds=timeout,
        )

    def embedding_for_lease(self, lease: CredentialLease) -> GuidelineEmbeddingPort | None:
        if self.desired_profile is None:
            return None
        return self._adapter(lease, self.policy.online_timeout_seconds)

    def bind_run_embedding(self, run_id: str, port: GuidelineEmbeddingPort | None) -> None:
        if run_id in self._run_embeddings:
            raise ValueError("guideline run embedding is already bound")
        self._run_embeddings[run_id] = port

    def embedding_for_run(self, run_id: str) -> GuidelineEmbeddingPort | None:
        return self._run_embeddings.get(run_id)

    def release_run_embedding(self, run_id: str) -> None:
        self._run_embeddings.pop(run_id, None)

    async def embedding_for_attempt(self) -> GuidelineEmbeddingPort:
        if self._stopping.is_set():
            raise PortConfigurationError("Guideline runtime is shutting down.", operation="embed")
        if self.desired_profile is None:
            raise PortConfigurationError("Guideline embedding configuration is unavailable.", operation="embed")
        try:
            lease = await self._credentials.lease(CredentialName.OPENAI)
        except (CredentialMissingError, CredentialStoreUnavailableError):
            raise PortAuthenticationError("Guideline embedding credential is unavailable.", operation="embed") from None
        return self._adapter(lease, self.policy.worker_timeout_seconds)

    async def start(self, repository: GuidelineRepositoryPort | None, registry: ParameterCapabilityRegistry,
                    *, source: BundledGuidelineSourcePort | None = None) -> None:
        if self._started or self._stopping.is_set():
            raise RuntimeError("guideline runtime cannot be restarted")
        self._started = True
        if repository is None:
            return
        self.import_report = await BundledGuidelineImporter(
            source if source is not None else PackageBundledGuidelineSource(registry=registry), repository,
        ).import_all()
        self.worker = GuidelinePreparationWorker(repository, registry, self.desired_profile,
            self.policy, self.embedding_for_attempt, lease_seconds=self._lease_seconds)
        if self.desired_profile is not None:
            self._task = asyncio.create_task(self._run(), name="guideline-preparation-worker")

    async def _run(self):
        while not self._stopping.is_set():
            try:
                await self.worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.safe_error_code = guideline_embedding_failure(error)[0]
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def shutdown(self) -> None:
        self._stopping.set()
        if self.worker is not None:
            self.worker.request_stop()
        if self._task is not None:
            if self._task.done():
                await asyncio.gather(self._task, return_exceptions=True)
                return
            try:
                async with asyncio.timeout(self._shutdown_grace):
                    await asyncio.shield(self._task)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
