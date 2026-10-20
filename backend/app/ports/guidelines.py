from typing import Protocol
from uuid import UUID

from app.domain.guidelines.catalog_snapshot import GuidelineCatalogSnapshot
from app.domain.guidelines.canonical import Revision
from app.domain.guidelines.canonical_document import CanonicalGuidelineDocumentV1
from app.domain.guidelines.canonical_query import CanonicalGuidelineQueryV1
from app.domain.guidelines.document_snapshot import GuidelineDocumentSnapshot
from app.domain.guidelines.draft import GuidelineDraftV1
from app.domain.guidelines.embedding_batch import GuidelineEmbeddingBatch
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.mutation import GuidelineMutation
from app.domain.guidelines.mutation_receipt import GuidelineMutationReceipt
from app.domain.guidelines.operation import GuidelineOperation
from app.domain.guidelines.version import GuidelineVersionV1
from app.domain.hydrology.parameter_key import ParameterKey
from app.ports.embedding import EmbeddingCancellationPort


class GuidelineRepositoryPort(Protocol):
    """Mutations replay a matching receipt before checking the expected revision."""

    async def read_document(self, document_id: UUID) -> GuidelineDocumentSnapshot | None: ...

    async def read_version(self, document_id: UUID, version_id: UUID) -> GuidelineVersionV1 | None: ...

    async def read_operation(self, operation_id: UUID) -> GuidelineOperation | None: ...

    async def read_receipt(self, mutation: GuidelineMutation) -> GuidelineMutationReceipt | None: ...

    async def create_draft(
        self, identity: GuidelineIdentity, draft: GuidelineDraftV1, mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def save_draft(
        self, document_id: UUID, draft: GuidelineDraftV1, *, expected_revision: Revision,
        mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def publish_version(
        self, version: GuidelineVersionV1, *, expected_revision: Revision,
        mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def read_catalog(self) -> GuidelineCatalogSnapshot: ...

    async def read_effective_versions(
        self, snapshot: GuidelineCatalogSnapshot, parameter_keys: tuple[ParameterKey, ...],
    ) -> tuple[GuidelineVersionV1, ...]: ...


class BundledGuidelineSourcePort(Protocol):
    def read_versions(self) -> tuple[GuidelineVersionV1, ...]: ...


class GuidelineEmbeddingPort(Protocol):
    async def embed(
        self, profile: GuidelineEmbeddingProfileV1,
        inputs: tuple[CanonicalGuidelineDocumentV1 | CanonicalGuidelineQueryV1, ...],
        deadline: float | None, cancel_token: EmbeddingCancellationPort | None,
    ) -> GuidelineEmbeddingBatch: ...
