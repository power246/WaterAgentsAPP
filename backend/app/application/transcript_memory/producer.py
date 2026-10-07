from __future__ import annotations

from app.application.canonical_json import canonical_json_digest
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.transcript_memory.search import (
    RAGSearchInvalidError,
    RAGSearchService,
    RAGSearchSession,
    RAGSearchUnavailableError,
)
from app.domain import (
    CollectionCompletionContract,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    HistoricalTranscriptRecord,
    MemoryMode,
    ProviderCollectionBatch,
    TranscriptEvidenceRequest,
    canonical_model_bytes,
)


class TranscriptEvidenceProducer:
    """The sole M5 adapter over session-bound M8 historical search."""

    def __init__(self, search: RAGSearchService) -> None:
        self._search = search
        self._scopes: dict[tuple[str, str], EvidenceSessionScope] = {}
        self._sessions: dict[tuple[str, str], RAGSearchSession] = {}

    def prepare_scope(self, scope: EvidenceSessionScope) -> None:
        if scope.memory_mode is not MemoryMode.MEMORY_AUGMENTED:
            return
        key = (scope.run_id, scope.session_id)
        existing = self._scopes.get(key)
        if existing is not None:
            if not _same_search_scope(existing, scope):
                raise ValueError("historical search scope identity collision")
            return
        self._scopes[key] = scope
        self._sessions[key] = self._search.bind(scope)

    async def collect(
        self,
        request: TranscriptEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[HistoricalTranscriptRecord]:
        key = (request.run_id, request.session_id)
        scope = self._scopes.get(key)
        session = self._sessions.get(key)
        if (
            scope is None
            or session is None
            or request.memory_mode is not MemoryMode.MEMORY_AUGMENTED
            or scope.memory_mode is not request.memory_mode
        ):
            return _failed_batch(request.operation_id, "index_invalid")
        try:
            records = await session.search(
                request.intent,
                operation_id=request.operation_id,
                deadline=deadline,
            )
        except RAGSearchUnavailableError as error:
            return _failed_batch(
                request.operation_id,
                _unavailable_code(error.code),
            )
        except RAGSearchInvalidError as error:
            return _failed_batch(
                request.operation_id,
                _invalid_code(error.code),
            )
        return _completed_batch(request.operation_id, records)

    def cleanup_run(self, run_id: str) -> None:
        keys = tuple(key for key in self._sessions if key[0] == run_id)
        for key in keys:
            self._sessions.pop(key).clear()
            self._scopes.pop(key, None)


def _same_search_scope(
    existing: EvidenceSessionScope,
    candidate: EvidenceSessionScope,
) -> bool:
    return (
        existing.cancel_token is candidate.cancel_token
        and existing.agent_comid == candidate.agent_comid
        and existing.topology == candidate.topology
        and existing.time_windows == candidate.time_windows
        and existing.requested_parameters == candidate.requested_parameters
        and existing.supporting_parameters == candidate.supporting_parameters
        and existing.memory_mode is candidate.memory_mode
        and existing.mode is candidate.mode
        and existing.analysis_timezone == candidate.analysis_timezone
        and existing.time_resolution == candidate.time_resolution
    )


def _completed_batch(
    operation_id: str,
    records: tuple[HistoricalTranscriptRecord, ...],
) -> ProviderCollectionBatch[HistoricalTranscriptRecord]:
    response_bytes = sum(len(canonical_model_bytes(record)) for record in records)
    page_identity = f"transcript:{operation_id}"
    page = CollectionPageManifest(
        page_identity=page_identity,
        record_count=len(records),
        response_bytes=response_bytes,
        page_complete=True,
    )
    proof_values = {
        "contract": CollectionCompletionContract.SINGLE_RESPONSE.value,
        "complete": True,
        "terminal_identity": page_identity,
        "pages": (page.model_dump(mode="json"),),
    }
    return ProviderCollectionBatch[HistoricalTranscriptRecord](
        operation_id=operation_id,
        records=records,
        page_or_chunk_manifests=(page,),
        completed_ranges=(),
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=response_bytes,
        ),
        completion_proof=CollectionCompletionProof(
            contract=CollectionCompletionContract.SINGLE_RESPONSE,
            complete=True,
            terminal_identity=page_identity,
            digest=canonical_json_digest(proof_values),
        ),
    )


def _failed_batch(
    operation_id: str,
    safe_code: str,
) -> ProviderCollectionBatch[HistoricalTranscriptRecord]:
    proof_values = {
        "contract": CollectionCompletionContract.SINGLE_RESPONSE.value,
        "complete": False,
        "safe_code": safe_code,
    }
    return ProviderCollectionBatch[HistoricalTranscriptRecord](
        operation_id=operation_id,
        records=(),
        page_or_chunk_manifests=(),
        completed_ranges=(),
        counts=CollectionCounts(requests=1),
        completion_proof=CollectionCompletionProof(
            contract=CollectionCompletionContract.SINGLE_RESPONSE,
            complete=False,
            digest=canonical_json_digest(proof_values),
        ),
        incomplete_reason=safe_code,
    )


def _unavailable_code(code: str) -> str:
    if code in {"index_building", "embedding_unavailable"}:
        return code
    return "historical_index_unavailable"


def _invalid_code(code: str) -> str:
    if code == "embedding_protocol_invalid":
        return code
    if code.startswith("historical_source_"):
        return "historical_source_invalid"
    return "index_invalid"
