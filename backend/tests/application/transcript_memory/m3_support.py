from datetime import UTC, datetime
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionResultReference,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.query_understanding import AnalysisContext
from app.application.transcript_memory import (
    ConfirmedSourceReference,
    RAGCaseSource,
    TypedRAGSourceItem,
)
from app.application.transcripts import create_production_registry
from app.application.transcripts.models import StoredTranscriptItem
from app.domain import (
    AnalysisDetail,
    CollectionStatus,
    CoverageStatus,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceChain,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceReference,
    EvidenceResultEligibility,
    InferenceResult,
    Provenance,
    ResultSource,
    SourceRecordReference,
    TimeSegment,
    WaterQualityParameter,
    build_time_windows,
)
from tests.application.evidence_tools.conftest import (
    TARGET_PARAMETER,
    make_context,
    make_graph,
)
from tests.domain.test_results import make_result, make_series

RUN_ID = UUID("81000000-0000-4000-8000-000000000001")
CONTEXT_ITEM_ID = UUID("81000000-0000-4000-8000-000000000002")
TOPOLOGY_ITEM_ID = UUID("81000000-0000-4000-8000-000000000003")
SESSION_ID = UUID("81000000-0000-4000-8000-000000000010")
FRAGMENT_ITEM_ID = UUID("81000000-0000-4000-8000-000000000011")
RESULT_ITEM_ID = UUID("81000000-0000-4000-8000-000000000012")
SOURCE_RECORD_ITEM_ID = UUID("81000000-0000-4000-8000-000000000013")
GENERATION_ID = "81000000-0000-4000-8000-000000000020"
PROFILE_ID = "sha256:test-profile"
NOW = datetime(2026, 10, 8, 1, tzinfo=UTC)
REGISTRY = create_production_registry()


def case_source(
    *,
    target_comid: str = "123",
    session_id: UUID = SESSION_ID,
    fragment_item_id: UUID = FRAGMENT_ITEM_ID,
    result_item_id: UUID = RESULT_ITEM_ID,
    source_record_item_id: UUID = SOURCE_RECORD_ITEM_ID,
    context: AnalysisContext | None = None,
    query: str | None = None,
) -> RAGCaseSource:
    resolved_context = context or make_context()
    if query is not None:
        values = resolved_context.model_dump(mode="python")
        values["query"] = query
        resolved_context = AnalysisContext.model_validate(values)
    topology = make_graph(with_upstream=True)
    result = _result(
        resolved_context,
        target_comid=target_comid,
        session_id=session_id,
        fragment_item_id=fragment_item_id,
    )
    fragment = _fragment(
        resolved_context,
        target_comid=target_comid,
        source_record_item_id=source_record_item_id,
    )
    context_item = typed_item(
        content_type="query_understood",
        body=resolved_context,
        item_id=CONTEXT_ITEM_ID,
        session_id=None,
        sequence=3,
    )
    topology_item = typed_item(
        content_type="topology_ready",
        body=topology,
        item_id=TOPOLOGY_ITEM_ID,
        session_id=None,
        sequence=4,
    )
    fragment_item = typed_item(
        content_type="evidence_fragment",
        body=fragment,
        item_id=fragment_item_id,
        session_id=session_id,
        sequence=8,
    )
    result_item = typed_item(
        content_type="inference_result",
        body=result,
        item_id=result_item_id,
        session_id=session_id,
        sequence=9,
    )
    session = SessionSummary(
        session_id=session_id,
        agent_role=AgentRole.WATERBODY,
        target_comid=target_comid,
        status=AgentSessionStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
        result_ref=SessionResultReference(run_id=RUN_ID, item_id=result_item_id),
        result_summary=WaterbodySessionResultSummary(
            result_id=result.result_id,
            target_comid=target_comid,
            risk_level=result.overall_assessment.risk_level,
            availability=result.overall_assessment.availability,
            overall_confidence=result.overall_confidence,
        ),
    )
    return RAGCaseSource(
        run_status=RunStatus.COMPLETED,
        session=session,
        persistence_confirmed=True,
        analysis_context=context_item,
        topology=topology_item,
        result=result_item,
        fragments=(fragment_item,),
        confirmed_references=(
            ConfirmedSourceReference(
                run_id=RUN_ID,
                item_id=fragment_item_id,
                session_id=session_id,
                content_type="evidence_fragment",
                content_schema_version=1,
            ),
            ConfirmedSourceReference(
                run_id=RUN_ID,
                item_id=source_record_item_id,
                session_id=session_id,
                content_type="evidence_source_records",
                content_schema_version=1,
            ),
        ),
    )


def typed_item(
    *,
    content_type: str,
    body,
    item_id: UUID,
    session_id: UUID | None,
    sequence: int,
) -> TypedRAGSourceItem:
    content = REGISTRY.encode(content_type, 1, body)
    item = StoredTranscriptItem(
        item_id=item_id,
        run_id=RUN_ID,
        session_id=session_id,
        transcript_sequence=sequence,
        created_at=NOW,
        content_type=content_type,
        content_schema_version=1,
        tags=content.metadata.tags,
        rag_eligible=content.metadata.rag_eligible,
        inline_body=content.safe_body,
        content_bytes=content.content_bytes,
    )
    read = REGISTRY.parse(item, content.safe_body)
    assert read.body is not None
    return TypedRAGSourceItem(
        item=item,
        body=read.body,
        references=REGISTRY.references_from_read(read),
    )


def _result(
    context: AnalysisContext,
    *,
    target_comid: str,
    session_id: UUID,
    fragment_item_id: UUID,
) -> InferenceResult:
    parameter_intent = context.params_used[0]
    parameter = WaterQualityParameter(
        key=parameter_intent.key,
        name=parameter_intent.canonical_name,
        canonical_unit=parameter_intent.canonical_unit,
        category="chemical",
    )
    window_count = len(
        build_time_windows(
            context.time_range,
            context.time_resolution,
            context.analysis_timezone,
        )
    )
    series = make_series(
        context.time_range,
        resolution=context.time_resolution,
        resolution_source=context.time_resolution_source,
        point_sources=(ResultSource.PREDICTED,) * window_count,
        evidence_refs=(("evidence-1",),) * window_count,
        parameter=parameter,
    )
    series = series.model_copy(
        update={
            "points": tuple(
                point.model_copy(update={"unit_id": parameter.canonical_unit})
                for point in series.points
            )
        }
    )
    result = make_result(
        time_range=context.time_range,
        mode=context.mode,
        time_segments=tuple(
            TimeSegment(
                start=segment.time_range.start,
                end=segment.time_range.end,
                mode=segment.mode,
            )
            for segment in context.time_segments
        ),
        resolution=context.time_resolution,
        resolution_source=context.time_resolution_source,
        series=(series,),
        analysis_detail=AnalysisDetail(
            summary="Validated result fixture.",
            steps=(),
            transcript_item_refs=(str(fragment_item_id),),
        ),
        evidence_chain=EvidenceChain(
            references=(
                EvidenceReference(
                    evidence_id="evidence-1",
                    role="result_support",
                    summary="Validated supporting evidence.",
                ),
            )
        ),
    )
    values = result.model_dump(mode="python")
    values.update(
        run_id=RUN_ID,
        session_id=session_id,
        target_comid=target_comid,
        result_id=f"result-{target_comid}",
    )
    return InferenceResult.model_validate(values)


def _fragment(
    context: AnalysisContext,
    *,
    target_comid: str,
    source_record_item_id: UUID,
) -> EvidenceFragment:
    reference = SourceRecordReference(
        item_id=str(source_record_item_id),
        record_id="record-1",
    )
    duration = int((context.time_range.end - context.time_range.start).total_seconds())
    return EvidenceFragment(
        evidence_id="evidence-1",
        source="test_provider",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid=target_comid,
        location=EvidenceLocation(
            point=None,
            comid=target_comid,
            station_id="station-1",
            distance_to_target_km=None,
        ),
        time_range=context.time_range,
        parameter_refs=(TARGET_PARAMETER,),
        data_type="water_observation",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity="series-1",
        quality_class=EvidenceQualityClass.SUSPICIOUS,
        quality_flags=("provider_flagged",),
        coverage=EvidenceCoverage(
            status=CoverageStatus.COMPLETE,
            observed_count=1,
            expected_count=1,
            ratio="1",
            covered_duration_seconds=duration,
            window_duration_seconds=duration,
            largest_gap_seconds=0,
        ),
        payload=DirectRecordPayload(
            record_ref=reference,
            displayed_value="8",
            displayed_unit=context.params_used[0].canonical_unit,
        ),
        source_record_refs=(reference,),
        provenance=Provenance(
            operation_id="operation-1",
            registry_digest="sha256:registry",
            provider_record_id="private-provider-record",
        ),
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        rag_eligible=True,
    )
