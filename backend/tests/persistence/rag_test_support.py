from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import (
    AssessmentAvailability,
    EmbeddingProfile,
    EmbeddingVector,
    HistoricalCaseRef,
    HistoricalResultCellProjection,
    HistoricalResultProjection,
    InferenceMode,
    OutcomeStatus,
    ParameterKey,
    RAGCaseProjectionV1,
    RAGFragmentProjectionV1,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGQualityTier,
    ResultSource,
    RiskLevel,
    TimeRange,
    TrendDirection,
)
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_items,
)


@dataclass(frozen=True)
class TranscriptSourceFixture:
    run_id: UUID
    session_id: UUID
    analysis_context_item_id: UUID
    fragment_item_id: UUID
    result_item_id: UUID
    completed_at: datetime


async def seed_completed_source(
    engine: AsyncEngine,
    *,
    target_comid: str = "12345678",
) -> TranscriptSourceFixture:
    run_id = uuid4()
    session_id = uuid4()
    request_item_id = uuid4()
    run_created_item_id = uuid4()
    analysis_context_item_id = uuid4()
    session_created_item_id = uuid4()
    fragment_item_id = uuid4()
    result_item_id = uuid4()
    session_terminal_item_id = uuid4()
    run_terminal_item_id = uuid4()
    created_at = datetime(2026, 10, 7, 8, tzinfo=UTC)
    completed_at = created_at + timedelta(minutes=1)
    async with engine.begin() as connection:
        await connection.execute(
            insert(analysis_runs).values(
                run_id=run_id,
                status="completed",
                created_at=created_at,
                updated_at=completed_at,
                query="database fixture",
                creation_receipt={},
                request_item_id=request_item_id,
                created_item_id=run_created_item_id,
                terminal_item_id=run_terminal_item_id,
            )
        )
        await connection.execute(
            insert(analysis_sessions).values(
                session_id=session_id,
                run_id=run_id,
                agent_role="waterbody",
                target_comid=target_comid,
                status="completed",
                created_at=created_at,
                updated_at=completed_at,
                created_item_id=session_created_item_id,
                terminal_item_id=session_terminal_item_id,
            )
        )
        item_specs = (
            (request_item_id, None, "analysis_request", False),
            (run_created_item_id, None, "lifecycle_event", False),
            (analysis_context_item_id, None, "analysis_context", False),
            (session_created_item_id, session_id, "lifecycle_event", False),
            (fragment_item_id, session_id, "evidence_fragment", True),
            (result_item_id, session_id, "inference_result", True),
            (session_terminal_item_id, session_id, "lifecycle_event", False),
            (run_terminal_item_id, None, "lifecycle_event", False),
        )
        await connection.execute(
            insert(transcript_items),
            [
                {
                    "item_id": item_id,
                    "run_id": run_id,
                    "session_id": item_session_id,
                    "transcript_sequence": sequence,
                    "created_at": created_at + timedelta(seconds=sequence),
                    "content_type": content_type,
                    "content_schema_version": 1,
                    "tags": [],
                    "rag_eligible": rag_eligible,
                    "inline_body": "{}",
                    "content_bytes": 2,
                }
                for sequence, (
                    item_id,
                    item_session_id,
                    content_type,
                    rag_eligible,
                ) in enumerate(item_specs, 1)
            ],
        )
    return TranscriptSourceFixture(
        run_id=run_id,
        session_id=session_id,
        analysis_context_item_id=analysis_context_item_id,
        fragment_item_id=fragment_item_id,
        result_item_id=result_item_id,
        completed_at=completed_at,
    )


def embedding_profile(*, dimensions: int = 3) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="test-provider",
        model=f"embedding-{dimensions}",
        output_dimensions=dimensions,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )


def pending_job(
    source: TranscriptSourceFixture,
    generation_id: str,
    *,
    job_id: UUID | None = None,
) -> RAGIndexJob:
    now = datetime.now(UTC)
    return RAGIndexJob(
        job_id=str(job_id or uuid4()),
        generation_id=generation_id,
        source_run_id=str(source.run_id),
        source_session_id=str(source.session_id),
        source_result_item_id=str(source.result_item_id),
        status=RAGIndexJobStatus.PENDING,
        attempts=0,
        created_at=now,
        updated_at=now,
    )


def ready_projection(
    source: TranscriptSourceFixture,
    generation_id: str,
    profile: EmbeddingProfile,
    *,
    case_id: UUID | None = None,
) -> tuple[RAGCaseProjectionV1, tuple[RAGFragmentProjectionV1, ...]]:
    case_ref = HistoricalCaseRef(
        generation_id=generation_id,
        case_id=str(case_id or uuid4()),
        run_id=str(source.run_id),
        session_id=str(source.session_id),
        target_comid="12345678",
        analysis_context_item_id=str(source.analysis_context_item_id),
        result_item_id=str(source.result_item_id),
    )
    parameter = ParameterKey(code_system="usgs", code="00300")
    time_range = TimeRange(
        start=datetime(2026, 7, 10, tzinfo=UTC),
        end=datetime(2026, 7, 17, tzinfo=UTC),
    )
    cell = HistoricalResultCellProjection(
        parameter=parameter,
        window_id="window-0001",
        time_range=time_range,
        outcome_status=OutcomeStatus.DIRECTIONAL,
        trend=TrendDirection.FALLING,
        risk_level=RiskLevel.MEDIUM,
        confidence=0.72,
        source=ResultSource.PREDICTED,
    )
    result = HistoricalResultProjection(
        mode=InferenceMode.PREDICTION,
        source_completed_at=source.completed_at,
        overall_risk=RiskLevel.MEDIUM,
        availability=AssessmentAvailability.PARTIAL,
        cells=(cell,),
    )
    case = RAGCaseProjectionV1(
        case_ref=case_ref,
        profile_id=profile.profile_id,
        mode=InferenceMode.PREDICTION,
        parameter_keys=(parameter,),
        window_ids=(cell.window_id,),
        source_completed_at=source.completed_at,
        canonical_query="schema=1;parameter=usgs:00300",
        canonical_query_digest="sha256:canonical-query",
        result_projection=result,
        projection_digest="sha256:case-projection",
        embedding=EmbeddingVector(values=tuple(0.1 for _ in range(profile.output_dimensions))),
    )
    fragment = RAGFragmentProjectionV1(
        case_ref=case_ref,
        source_fragment_item_id=str(source.fragment_item_id),
        evidence_id="evidence-1",
        parameter=parameter,
        window_id=cell.window_id,
        valid_time_range=time_range,
        quality_tier=RAGQualityTier.VALID,
        applicability="direct_measurement",
        provenance_identity="sha256:provenance",
        projection_digest="sha256:fragment-projection",
    )
    return case, (fragment,)
