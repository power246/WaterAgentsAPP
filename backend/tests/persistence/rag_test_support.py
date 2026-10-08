from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import (
    AssessmentAvailability,
    CanonicalCaseQueryV1,
    CanonicalIntentToken,
    CanonicalWindowDescriptorV1,
    CoverageStatus,
    EmbeddingProfile,
    EmbeddingVector,
    EvidenceCoverage,
    EvidenceResultEligibility,
    HistoricalCaseRef,
    HistoricalResultCellProjection,
    HistoricalResultProjection,
    InferenceMode,
    MeteorologicalSeason,
    OutcomeStatus,
    ParameterKey,
    RAGCaseProjectionV1,
    RAGCaseWindowV1,
    RAGFragmentProjectionV1,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGQualityTier,
    ResultSource,
    RiskLevel,
    TimeRange,
    TimeResolution,
    TrendDirection,
)
from app.domain.memory_rag.historical_case_identity import derive_historical_case_id
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
    parser_session_id = uuid4()
    session_id = uuid4()
    request_item_id = uuid4()
    run_created_item_id = uuid4()
    analysis_context_item_id = uuid4()
    parser_created_item_id = uuid4()
    parser_terminal_item_id = uuid4()
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
        await connection.execute(
            insert(analysis_sessions).values(
                session_id=parser_session_id,
                run_id=run_id,
                agent_role="query_parser",
                target_comid=None,
                status="completed",
                created_at=created_at,
                updated_at=completed_at,
                created_item_id=parser_created_item_id,
                terminal_item_id=parser_terminal_item_id,
            )
        )
        item_specs = (
            (request_item_id, None, "analysis_request", False),
            (run_created_item_id, None, "lifecycle_event", False),
            (parser_created_item_id, parser_session_id, "lifecycle_event", False),
            (analysis_context_item_id, parser_session_id, "query_understood", False),
            (parser_terminal_item_id, parser_session_id, "lifecycle_event", False),
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
        case_id=str(
            case_id
            or derive_historical_case_id(
                generation_id,
                source.run_id,
                source.session_id,
                source.result_item_id,
            )
        ),
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
        overall_confidence=0.8,
        availability=AssessmentAvailability.PARTIAL,
        cells=(cell,),
    )
    canonical = CanonicalCaseQueryV1(
        mode=InferenceMode.PREDICTION,
        requested_parameter_keys=(parameter,),
        window_descriptors=(
            CanonicalWindowDescriptorV1(
                window_kind=InferenceMode.PREDICTION,
                duration_days="7",
                local_months=(7,),
                season_buckets=(MeteorologicalSeason.JJA,),
                resolution=TimeResolution.TWENTY_FOUR_HOURS,
            ),
        ),
        normalized_intent_tokens=(
            CanonicalIntentToken.ANALYZE_CURRENT_STATE,
            CanonicalIntentToken.PREDICT,
            CanonicalIntentToken.REQUIRES_DISCHARGE_SUPPORT,
        ),
    )
    case = RAGCaseProjectionV1(
        case_ref=case_ref,
        profile_id=profile.profile_id,
        mode=InferenceMode.PREDICTION,
        parameter_keys=(parameter,),
        window_ids=(cell.window_id,),
        windows=(
            RAGCaseWindowV1(
                window_id=cell.window_id,
                time_range=time_range,
                window_kind=InferenceMode.PREDICTION,
                local_months=(7,),
                season_buckets=(MeteorologicalSeason.JJA,),
                season_year=2026,
                duration_seconds=int((time_range.end - time_range.start).total_seconds()),
                resolution=TimeResolution.TWENTY_FOUR_HOURS,
                parameter_keys=(parameter,),
            ),
        ),
        source_completed_at=source.completed_at,
        canonical_query=canonical.canonical_text,
        canonical_query_digest=canonical.canonical_digest,
        result_projection=result,
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
        coverage=EvidenceCoverage(
            status=CoverageStatus.PARTIAL,
            observed_count=1,
            expected_count=None,
            ratio=None,
            covered_duration_seconds=0,
            window_duration_seconds=int((time_range.end - time_range.start).total_seconds()),
            largest_gap_seconds=None,
        ),
        applicability="direct_measurement",
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        provenance_identity="sha256:provenance",
    )
    return case, (fragment,)
