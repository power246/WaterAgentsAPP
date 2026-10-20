from typing import Protocol
from datetime import datetime
from uuid import UUID

from app.domain.guidelines.catalog_snapshot import GuidelineCatalogSnapshot
from app.domain.guidelines.canonical import Digest, Revision
from app.domain.guidelines.canonical_document import CanonicalGuidelineDocumentV1
from app.domain.guidelines.canonical_query import CanonicalGuidelineQueryV1
from app.domain.guidelines.document_snapshot import GuidelineDocumentSnapshot
from app.domain.guidelines.document_details import GuidelineDocumentDetails
from app.domain.guidelines.list_item import GuidelineListItem
from app.domain.guidelines.draft import GuidelineDraftV1
from app.domain.guidelines.embedding_batch import GuidelineEmbeddingBatch
from app.domain.guidelines.embedding_profile import GuidelineEmbeddingProfileV1
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.index_generation import GuidelineIndexGeneration
from app.domain.guidelines.mutation import GuidelineMutation
from app.domain.guidelines.mutation_receipt import GuidelineMutationReceipt
from app.domain.guidelines.operation import GuidelineOperation
from app.domain.guidelines.version import GuidelineVersionV1
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.ports.embedding import EmbeddingCancellationPort


class GuidelineRepositoryPort(Protocol):
    """Mutations replay a matching receipt before checking the expected revision."""

    async def read_document(self, document_id: UUID) -> GuidelineDocumentSnapshot | None: ...

    async def read_document_details(self, document_id: UUID) -> GuidelineDocumentDetails | None: ...

    async def list_documents(
        self, *, source: str, state: str | None, q: str, after_id: UUID | None, limit: int,
    ) -> tuple[GuidelineListItem, ...]: ...

    async def read_version(self, document_id: UUID, version_id: UUID) -> GuidelineVersionV1 | None: ...

    async def read_operation(self, operation_id: UUID) -> GuidelineOperation | None: ...

    async def read_receipt(self, mutation: GuidelineMutation) -> GuidelineMutationReceipt | None: ...

    async def create_draft(
        self, identity: GuidelineIdentity, draft: GuidelineDraftV1, mutation: GuidelineMutation,
        *, expected_base_revision: Revision | None = None,
    ) -> GuidelineMutationReceipt: ...

    async def save_draft(
        self, document_id: UUID, draft: GuidelineDraftV1, *, expected_revision: Revision,
        mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def publish_version(
        self, version: GuidelineVersionV1, *, expected_revision: Revision,
        mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def import_bundled_version(self, version: GuidelineVersionV1) -> GuidelineMutationReceipt: ...

    async def local_edit(
        self, document_id: UUID, base_version_id: UUID, base_content_digest: Digest, *,
        expected_revision: Revision, mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def read_catalog(self) -> GuidelineCatalogSnapshot: ...

    async def read_effective_versions(
        self, snapshot: GuidelineCatalogSnapshot, parameter_keys: tuple[ParameterKey, ...],
    ) -> tuple[GuidelineVersionV1, ...]: ...

    async def set_enabled(
        self, document_id: UUID, enabled: bool, *, expected_revision: Revision, mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def restore_bundled(
        self, document_id: UUID, version_id: UUID, *, expected_revision: Revision, mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def read_generation(self, generation_id: UUID) -> GuidelineIndexGeneration | None: ...

    async def create_generation(self, profile: GuidelineEmbeddingProfileV1) -> GuidelineIndexGeneration: ...

    async def ensure_generation(self, profile: GuidelineEmbeddingProfileV1) -> GuidelineIndexGeneration: ...

    async def queue_generation(self, generation_id: UUID, *, limit: int = 64) -> None: ...

    async def read_embedding(self, generation_id: UUID, version_id: UUID) -> EmbeddingVector | None: ...

    async def activate_generation(self, generation_id: UUID, *, expected_catalog_revision: Revision) -> None: ...

    async def claim_operation(
        self, owner: str, *, now: datetime | None = None, lease_seconds: int = 60,
        generation_id: UUID | None = None,
    ) -> GuidelineOperation | None: ...

    async def fail_attempt(
        self, claim: GuidelineOperation, safe_error_code: str, *, retryable: bool, now: datetime | None = None,
    ) -> None: ...

    async def retry_operation(
        self, operation_id: UUID, *, expected_revision: Revision, mutation: GuidelineMutation,
    ) -> GuidelineMutationReceipt: ...

    async def write_embedding(
        self, claim: GuidelineOperation, generation_id: UUID,
        canonical_document: CanonicalGuidelineDocumentV1, vector: EmbeddingVector,
        *, now: datetime | None = None,
    ) -> None: ...

    async def activate_version(self, claim: GuidelineOperation, *, now: datetime | None = None) -> None: ...

    async def complete_operation(
        self, claim: GuidelineOperation, generation_id: UUID,
        canonical_document: CanonicalGuidelineDocumentV1, vector: EmbeddingVector,
        *, now: datetime | None = None,
    ) -> None: ...

    async def write_building_embedding(
        self, generation_id: UUID, version_id: UUID,
        canonical_document: CanonicalGuidelineDocumentV1, vector: EmbeddingVector,
    ) -> None: ...


class BundledGuidelineSourcePort(Protocol):
    def read_versions(self) -> tuple[GuidelineVersionV1, ...]: ...


class GuidelineEmbeddingPort(Protocol):
    async def embed(
        self, profile: GuidelineEmbeddingProfileV1,
        inputs: tuple[CanonicalGuidelineDocumentV1 | CanonicalGuidelineQueryV1, ...],
        deadline: float | None, cancel_token: EmbeddingCancellationPort | None,
    ) -> GuidelineEmbeddingBatch: ...
