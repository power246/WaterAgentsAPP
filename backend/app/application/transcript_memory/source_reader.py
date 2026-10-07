from dataclasses import dataclass
from hashlib import sha256
from typing import Generic, Protocol, TypeVar, cast
from uuid import UUID

from app.application.query_understanding.context_models import AnalysisContext
from app.application.transcripts.models import ReadTranscriptItem, StoredTranscriptItem
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.registry import TranscriptRegistry
from app.domain.domain_model import DomainModel
from app.domain.evidence.serialization import canonical_model_bytes
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.hydrology.hydrology_graph import HydrologyGraph
from app.domain.inference.inference_result import InferenceResult

SourceBodyT = TypeVar("SourceBodyT", bound=DomainModel)


class RegisteredItemReader(Protocol):
    async def read_registered_item(
        self,
        run_id: UUID,
        item_id: UUID,
        view: ReadView,
        *,
        session_id: UUID | None = None,
    ) -> ReadTranscriptItem: ...


class RAGSourceReadError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TypedRAGSourceItem(Generic[SourceBodyT]):
    item: StoredTranscriptItem
    body: SourceBodyT
    references: tuple[UUID, ...]

    @property
    def body_digest(self) -> str:
        return f"sha256:{sha256(canonical_model_bytes(self.body)).hexdigest()}"


class TypedRAGSourceReader:
    def __init__(
        self,
        reader: RegisteredItemReader,
        registry: TranscriptRegistry,
    ) -> None:
        self._reader = reader
        self._registry = registry

    async def read_analysis_context(
        self,
        run_id: UUID,
        item_id: UUID,
    ) -> TypedRAGSourceItem[AnalysisContext]:
        return await self._read(
            run_id,
            item_id,
            session_id=None,
            content_type="query_understood",
            body_type=AnalysisContext,
            require_run_global=True,
        )

    async def read_topology(
        self,
        run_id: UUID,
        item_id: UUID,
    ) -> TypedRAGSourceItem[HydrologyGraph]:
        return await self._read(
            run_id,
            item_id,
            session_id=None,
            content_type="topology_ready",
            body_type=HydrologyGraph,
            require_run_global=True,
        )

    async def read_result(
        self,
        run_id: UUID,
        session_id: UUID,
        item_id: UUID,
    ) -> TypedRAGSourceItem[InferenceResult]:
        return await self._read(
            run_id,
            item_id,
            session_id=session_id,
            content_type="inference_result",
            body_type=InferenceResult,
            require_rag_candidate=True,
        )

    async def read_fragment(
        self,
        run_id: UUID,
        session_id: UUID,
        item_id: UUID,
    ) -> TypedRAGSourceItem[EvidenceFragment]:
        return await self._read(
            run_id,
            item_id,
            session_id=session_id,
            content_type="evidence_fragment",
            body_type=EvidenceFragment,
            require_rag_candidate=True,
        )

    async def _read(
        self,
        run_id: UUID,
        item_id: UUID,
        *,
        session_id: UUID | None,
        content_type: str,
        body_type: type[SourceBodyT],
        require_run_global: bool = False,
        require_rag_candidate: bool = False,
    ) -> TypedRAGSourceItem[SourceBodyT]:
        read = await self._reader.read_registered_item(
            run_id,
            item_id,
            ReadView.HISTORY,
            session_id=session_id,
        )
        item = read.item
        if item.run_id != run_id or item.item_id != item_id:
            raise RAGSourceReadError("historical_source_identity_mismatch")
        if require_run_global and item.session_id is not None:
            raise RAGSourceReadError("historical_source_scope_mismatch")
        if require_rag_candidate and not item.rag_eligible:
            raise RAGSourceReadError("historical_source_not_rag_candidate")
        if (
            item.content_type != content_type
            or item.content_schema_version != 1
            or not isinstance(read.body, body_type)
        ):
            raise RAGSourceReadError("historical_source_schema_unsupported")
        return TypedRAGSourceItem(
            item=item,
            body=cast(SourceBodyT, read.body),
            references=self._registry.references_from_read(read),
        )
