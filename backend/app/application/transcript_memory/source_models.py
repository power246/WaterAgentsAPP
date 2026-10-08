from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.application.analysis_runs.models import RunStatus, SessionSummary
from app.application.query_understanding.context_models import AnalysisContext
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.hydrology.hydrology_graph import HydrologyGraph
from app.domain.inference.inference_result import InferenceResult

if TYPE_CHECKING:
    from app.application.transcript_memory.source_reader import TypedRAGSourceItem


@dataclass(frozen=True, slots=True)
class ConfirmedSourceReference:
    run_id: UUID
    item_id: UUID
    session_id: UUID | None
    content_type: str
    content_schema_version: int
    persistence_confirmed: bool = True


@dataclass(frozen=True, slots=True)
class RAGCaseSource:
    run_status: RunStatus
    session: SessionSummary
    persistence_confirmed: bool
    analysis_context: TypedRAGSourceItem[AnalysisContext]
    topology: TypedRAGSourceItem[HydrologyGraph]
    result: TypedRAGSourceItem[InferenceResult]
    fragments: tuple[TypedRAGSourceItem[EvidenceFragment], ...]
    confirmed_references: tuple[ConfirmedSourceReference, ...]
