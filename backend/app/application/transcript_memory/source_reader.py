from dataclasses import dataclass
from hashlib import sha256
from typing import Generic, Protocol, TypeVar, cast
from uuid import UUID

from app.application.query_understanding.context_models import AnalysisContext
from app.application.analysis_runs.models import AgentRole, AgentSessionStatus
from app.application.transcript_memory.source_models import (
    ConfirmedSourceReference,
    RAGCaseSource,
)
from app.application.transcripts.models import ReadTranscriptItem, StoredTranscriptItem
from app.application.transcripts.read_models import ReadView, RegisteredRunRead
from app.application.transcripts.reader import (
    TranscriptDatabaseUnavailableError,
    TranscriptRunNotFoundError,
)
from app.application.transcripts.registry import TranscriptRegistry
from app.domain.domain_model import DomainModel
from app.domain.evidence.serialization import canonical_model_bytes
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.hydrology.hydrology_graph import HydrologyGraph
from app.domain.inference.inference_result import InferenceResult
from app.domain.memory_rag.rag_index_job import RAGIndexJob
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef

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


class RegisteredRunReader(Protocol):
    async def read_registered_run(
        self,
        run_id: UUID,
        view: ReadView,
    ) -> RegisteredRunRead: ...


class RAGSourceReadError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RAGSourceUnavailableError(RuntimeError):
    pass


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
            require_session_owned=True,
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
        require_session_owned: bool = False,
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
        if require_session_owned and item.session_id is None:
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


class TranscriptRAGCaseSourceReader:
    """Rehydrate one job from the authoritative persisted transcript in one read."""

    def __init__(
        self,
        reader: RegisteredRunReader,
        registry: TranscriptRegistry,
    ) -> None:
        self._reader = reader
        self._registry = registry

    async def load_case_source(self, job: RAGIndexJob) -> RAGCaseSource:
        return await self._load_case_source(
            run_id_text=job.source_run_id,
            session_id_text=job.source_session_id,
            result_item_id_text=job.source_result_item_id,
        )

    async def load_historical_case_source(
        self,
        case_ref: HistoricalCaseRef,
    ) -> RAGCaseSource:
        return await self._load_case_source(
            run_id_text=case_ref.run_id,
            session_id_text=case_ref.session_id,
            result_item_id_text=case_ref.result_item_id,
        )

    async def _load_case_source(
        self,
        *,
        run_id_text: str,
        session_id_text: str,
        result_item_id_text: str,
    ) -> RAGCaseSource:
        try:
            run_id = UUID(run_id_text)
            session_id = UUID(session_id_text)
            result_item_id = UUID(result_item_id_text)
        except ValueError as error:
            raise RAGSourceReadError("historical_source_identity_invalid") from error

        try:
            snapshot = await self._reader.read_registered_run(run_id, ReadView.HISTORY)
        except TranscriptDatabaseUnavailableError as error:
            raise RAGSourceUnavailableError("historical source is unavailable") from error
        except TranscriptRunNotFoundError as error:
            raise RAGSourceReadError("historical_source_missing") from error
        session = next(
            (item for item in snapshot.sessions if item.session_id == session_id),
            None,
        )
        if session is None:
            raise RAGSourceReadError("historical_source_session_missing")

        result = self._one_typed_item(
            snapshot,
            item_id=result_item_id,
            content_type="inference_result",
            body_type=InferenceResult,
            session_id=session_id,
            require_rag_candidate=True,
        )
        context = self._one_analysis_context(snapshot)
        topology = self._one_typed_item(
            snapshot,
            content_type="topology_ready",
            body_type=HydrologyGraph,
            session_id=None,
        )

        by_id = {item.item.item_id: item for item in snapshot.items}
        confirmed: list[ConfirmedSourceReference] = []
        fragments: list[TypedRAGSourceItem[EvidenceFragment]] = []
        for reference_id in result.references:
            referenced = by_id.get(reference_id)
            if referenced is None:
                continue
            confirmed.append(
                ConfirmedSourceReference(
                    run_id=referenced.item.run_id,
                    item_id=referenced.item.item_id,
                    session_id=referenced.item.session_id,
                    content_type=referenced.item.content_type,
                    content_schema_version=referenced.item.content_schema_version,
                )
            )
            if referenced.item.content_type == "evidence_fragment":
                fragments.append(
                    self._typed_item(
                        referenced,
                        body_type=EvidenceFragment,
                        require_rag_candidate=True,
                    )
                )

        return RAGCaseSource(
            run_status=snapshot.run.status,
            session=session,
            persistence_confirmed=True,
            analysis_context=context,
            topology=topology,
            result=result,
            fragments=tuple(fragments),
            confirmed_references=tuple(confirmed),
        )

    def _one_analysis_context(
        self,
        snapshot: RegisteredRunRead,
    ) -> TypedRAGSourceItem[AnalysisContext]:
        matches = tuple(
            item
            for item in snapshot.items
            if item.item.content_type == "query_understood"
            and item.item.content_schema_version == 1
        )
        if len(matches) != 1:
            raise RAGSourceReadError("historical_source_closure_ambiguous")
        session_id = matches[0].item.session_id
        session = next(
            (
                item
                for item in snapshot.sessions
                if item.session_id == session_id
            ),
            None,
        )
        if (
            session_id is None
            or session is None
            or session.agent_role is not AgentRole.QUERY_PARSER
            or session.status is not AgentSessionStatus.COMPLETED
        ):
            raise RAGSourceReadError("historical_source_scope_mismatch")
        return self._typed_item(
            matches[0],
            body_type=AnalysisContext,
            require_rag_candidate=False,
        )

    def _one_typed_item(
        self,
        snapshot: RegisteredRunRead,
        *,
        content_type: str,
        body_type: type[SourceBodyT],
        session_id: UUID | None,
        item_id: UUID | None = None,
        require_rag_candidate: bool = False,
    ) -> TypedRAGSourceItem[SourceBodyT]:
        matches = tuple(
            item
            for item in snapshot.items
            if item.item.content_type == content_type
            and item.item.content_schema_version == 1
            and item.item.session_id == session_id
            and (item_id is None or item.item.item_id == item_id)
        )
        if len(matches) != 1:
            raise RAGSourceReadError("historical_source_closure_ambiguous")
        return self._typed_item(
            matches[0],
            body_type=body_type,
            require_rag_candidate=require_rag_candidate,
        )

    def _typed_item(
        self,
        read: ReadTranscriptItem,
        *,
        body_type: type[SourceBodyT],
        require_rag_candidate: bool,
    ) -> TypedRAGSourceItem[SourceBodyT]:
        if not isinstance(read.body, body_type):
            raise RAGSourceReadError("historical_source_schema_unsupported")
        if require_rag_candidate and not read.item.rag_eligible:
            raise RAGSourceReadError("historical_source_not_rag_candidate")
        return TypedRAGSourceItem(
            item=read.item,
            body=cast(SourceBodyT, read.body),
            references=self._registry.references_from_read(read),
        )
