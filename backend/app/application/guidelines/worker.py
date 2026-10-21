import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from app.application.guidelines.embedding import guideline_embedding_failure
from app.domain.guidelines import canonical_guideline_document, GuidelineEmbeddingBatch, GuidelineEmbeddingProfileV1
from app.domain.guidelines.policy import GuidelinePolicy
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.ports import EmbeddingCancelledError, InvalidPortResponseError, PortConfigurationError, PortError
from app.ports.errors import GuidelineConflictError
from app.ports.guidelines import GuidelineEmbeddingPort, GuidelineRepositoryPort


class GuidelinePreparationWorker:
    def __init__(self, repository: GuidelineRepositoryPort, registry: ParameterCapabilityRegistry,
                 profile: GuidelineEmbeddingProfileV1 | None, policy: GuidelinePolicy,
                 embedding_factory: Callable[[], Awaitable[GuidelineEmbeddingPort]], *, lease_seconds: int = 60) -> None:
        self.repository = repository
        self.registry = registry
        self.profile = profile
        self.policy = policy
        self.embedding_factory = embedding_factory
        self.lease_seconds = lease_seconds
        self._stopped = asyncio.Event()
        self._cycle = asyncio.Lock()
        self.last_error_code: str | None = None

    def is_requested(self) -> bool:
        return self._stopped.is_set()

    def request_stop(self) -> None:
        self._stopped.set()

    async def reconcile_generation(self):
        if self.is_requested() or self.profile is None:
            return None
        generation = await self.repository.ensure_generation(self.profile)
        if generation.state == "building":
            await self.repository.queue_generation(generation.generation_id)
            catalog = await self.repository.read_catalog()
            if not self.is_requested():
                try:
                    await self.repository.activate_generation(generation.generation_id, expected_catalog_revision=catalog.revision)
                except GuidelineConflictError as error:
                    if error.code not in {"guideline_revision_conflict", "guideline_generation_incomplete", "guideline_generation_conflict"}:
                        raise
        return generation

    async def run_once(self) -> int:
        async with self._cycle:
            if self.is_requested() or self.profile is None:
                return 0
            generation = await self.reconcile_generation()
            catalog = await self.repository.read_catalog()
            if generation is None or (catalog.generation_id is None and generation.state == "failed"):
                return 0
            tasks = []
            try:
                for _ in range(self.policy.worker_max_concurrency):
                    if self.is_requested():
                        break
                    claim = await self.repository.claim_operation(
                        "guideline-" + str(uuid4()), lease_seconds=self.lease_seconds,
                        generation_id=generation.generation_id if catalog.generation_id is None else None,
                    )
                    if claim is None:
                        break
                    if self.is_requested():
                        break
                    tasks.append(asyncio.create_task(self._prepare(claim)))
                if tasks:
                    await asyncio.gather(*tasks)
                if not self.is_requested():
                    await self.reconcile_generation()
                return len(tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def _prepare(self, claim):
        try:
            deadline = asyncio.get_running_loop().time() + self.policy.worker_timeout_seconds
            async with asyncio.timeout(self.policy.worker_timeout_seconds):
                version = await self.repository.read_version(claim.receipt.document_id, claim.receipt.version_id)
                if version is None:
                    raise PortConfigurationError("Approved guideline version is unavailable.")
                embedding = None
                # A concurrent switch may require one computation for the new profile.
                for _ in range(2):
                    if self.is_requested():
                        return
                    if claim.activation_epoch is None:
                        generation = await self.repository.read_generation(claim.generation_id)
                        if generation is None or generation.state != "building":
                            return
                        generation_id, profile = generation.generation_id, generation.profile
                    else:
                        catalog = await self.repository.read_catalog()
                        generation_id, profile = catalog.generation_id, catalog.profile
                        if generation_id is None:
                            raise PortConfigurationError("Guideline generation is unavailable.")
                    document = canonical_guideline_document(version.content, profile, self.registry)
                    vector = await self.repository.read_embedding(generation_id, version.version_id)
                    if vector is None:
                        if embedding is None:
                            embedding = await self.embedding_factory()
                        if self.is_requested():
                            return
                        result = await embedding.embed(profile, (document,), deadline, self)
                        if result.profile != profile or len(result.vectors) != 1:
                            raise InvalidPortResponseError("Guideline embedding batch does not match the approved version/profile.")
                        try:
                            vector = GuidelineEmbeddingBatch.model_validate(result.model_dump()).vectors[0]
                        except ValueError:
                            raise InvalidPortResponseError("Guideline embedding batch is invalid.") from None
                    if self.is_requested():
                        return
                    try:
                        await self.repository.complete_operation(claim, generation_id, document, vector)
                        self.last_error_code = None
                        return
                    except GuidelineConflictError as error:
                        if error.code != "guideline_generation_changed" or claim.activation_epoch is None:
                            raise
                raise GuidelineConflictError("guideline_generation_changed")
        except asyncio.CancelledError:
            raise
        except GuidelineConflictError as error:
            if error.code in {"guideline_stale_attempt", "guideline_superseded"}:
                return
            await self._record_failure(claim, "guideline_generation_changed", retryable=True)
        except Exception as error:
            if self.is_requested() and isinstance(error, (EmbeddingCancelledError, TimeoutError)):
                return
            code, retryable = guideline_embedding_failure(error)
            if isinstance(error, ValueError):
                code, retryable = guideline_embedding_failure(PortConfigurationError("invalid content"))
            await self._record_failure(claim, code, retryable=retryable)

    async def _record_failure(self, claim, code, *, retryable):
        self.last_error_code = code
        if self.is_requested():
            return
        try:
            await self.repository.fail_attempt(claim, code, retryable=retryable)
        except (PortError, ValueError):
            # The durable lease recovers a lost acknowledgement or failed write.
            pass
