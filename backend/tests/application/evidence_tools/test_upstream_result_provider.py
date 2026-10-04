import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.evidence_tools import (
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
    RunLocalUpstreamResultStore,
    TranscriptUpstreamInferenceResultReader,
)
from app.application.analysis_runs.models import AgentRole, AgentSessionStatus, SessionSummary
from app.domain import (
    AnalysisDetail,
    EvidenceBundle,
    EvidenceFragmentReference,
    EvidenceToolExecutionStatus,
    OutcomeStatus,
    WaterQualityParameter,
)
from app.application.transcripts.models import TranscriptContentRef
from tests.application.evidence_tools.test_executor import executor, request as tool_request
from tests.application.evidence_tools.test_upstream_result_store import (
    RUN_ID,
    FakeResultReader,
    dependency_outcome,
    inference_result,
    request,
    resolved_result,
    store_for,
    topology,
)
from tests.application.evidence_tools.fakes import MemoryEvidenceStore
from tests.domain.test_results import ANALYSIS_TIMEZONE, local_day


def test_usable_projection_preserves_result_identity_confidence_and_closure() -> None:
    async def exercise() -> None:
        reader = FakeResultReader()
        store = store_for(topology("chain"), reader)
        session_id = UUID("30000000-0000-0000-0000-000000000200")
        result = inference_result("200", session_id)
        outcome = dependency_outcome("200", session_id, result=result)
        assert outcome.result_ref is not None
        resolved = resolved_result(result)
        reader.values[outcome.result_ref.item_id] = resolved
        await store.publish(outcome)

        batch = await store.collect(
            request(
                agent_comid="300",
                upstreams=("200",),
                windows=("window-001", "window-000"),
            )
        )

        assert tuple(record.window_id for record in batch.records) == (
            "window-000",
            "window-001",
        )
        first = batch.records[0]
        assert first.upstream_session_id == str(session_id)
        assert first.result_ref.item_id == str(outcome.result_ref.item_id)
        assert first.parameter == result.requested_parameters[0]
        assert first.result_kind == "quantified"
        assert first.result_source.value == "inferred"
        assert first.confidence == result.series[0].points[0].confidence
        assert first.evidence_closure_refs == resolved.closure_refs
        assert batch.counts.records == 2
        assert batch.page_or_chunk_manifests[0].record_count == 2
        assert batch.completion_proof.complete is True

    asyncio.run(exercise())


def test_transcript_result_reader_validates_the_registered_same_session_closure() -> None:
    class RegisteredReader:
        def __init__(self, result_item_id, result) -> None:
            self.result_item_id = result_item_id
            self.result = result
            self.calls = []

        async def read_registered_session_items(
            self,
            run_id,
            session_id,
            view,
        ):
            self.calls.append((run_id, session_id, view))
            return (
                SimpleNamespace(
                    item=SimpleNamespace(
                        item_id=self.result_item_id,
                        content_type="inference_result",
                        content_schema_version=1,
                    ),
                    body=self.result,
                ),
                SimpleNamespace(
                    item=SimpleNamespace(
                        item_id=UUID(
                            self.result.analysis_detail.transcript_item_refs[0]
                        ),
                        content_type="evidence_bundle",
                        content_schema_version=1,
                    ),
                    body=SimpleNamespace(),
                ),
            )

    async def exercise() -> None:
        session_id = UUID("30000000-0000-0000-0000-000000000200")
        result = inference_result("200", session_id)
        outcome = dependency_outcome("200", session_id, result=result)
        assert outcome.result_ref is not None
        reader = RegisteredReader(outcome.result_ref.item_id, result)

        resolved = await TranscriptUpstreamInferenceResultReader(reader).resolve(
            outcome
        )

        assert resolved.result == result
        assert tuple(ref.item_id for ref in resolved.closure_refs) == (
            result.analysis_detail.transcript_item_refs[0],
        )
        assert reader.calls[0][0] == RUN_ID
        assert reader.calls[0][1] == session_id

    asyncio.run(exercise())


def test_transcript_result_reader_derives_production_closure_from_final_bundle() -> None:
    async def exercise() -> None:
        session_id = UUID("30000000-0000-0000-0000-000000000200")
        result = inference_result("200", session_id).model_copy(
            update={
                "analysis_detail": AnalysisDetail(
                    summary="Analysis complete",
                    steps=(),
                )
            }
        )
        outcome = dependency_outcome("200", session_id, result=result)
        assert outcome.result_ref is not None
        bundle_item_id = UUID("70000000-0000-0000-0000-000000000001")
        fragment_item_id = UUID("70000000-0000-0000-0000-000000000002")
        source_item_id = UUID("70000000-0000-0000-0000-000000000003")
        bundle = EvidenceBundle.model_construct(
            fragment_refs=(
                EvidenceFragmentReference(
                    item_id=str(fragment_item_id),
                    evidence_id="evidence-empty",
                ),
            ),
            source_record_item_refs=(str(source_item_id),),
            closure_refs=(),
            tool_result_refs=(),
        )
        items = (
            SimpleNamespace(
                item=SimpleNamespace(
                    item_id=outcome.result_ref.item_id,
                    content_type="inference_result",
                    content_schema_version=1,
                ),
                body=result,
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=bundle_item_id),
                body=bundle,
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=fragment_item_id),
                body=SimpleNamespace(),
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=source_item_id),
                body=SimpleNamespace(),
            ),
        )

        class RegisteredReader:
            async def read_registered_session_items(
                self,
                run_id,
                read_session_id,
                view,
            ):
                assert run_id == RUN_ID
                assert read_session_id == session_id
                return items

        resolved = await TranscriptUpstreamInferenceResultReader(
            RegisteredReader()
        ).resolve(outcome)

        assert tuple(reference.item_id for reference in resolved.closure_refs) == (
            str(bundle_item_id),
            str(fragment_item_id),
            str(source_item_id),
        )

    asyncio.run(exercise())


def test_real_port_keeps_m5_preflight_pure_and_audits_legal_collect_failure(
    scope_factory,
) -> None:
    async def exercise() -> None:
        run_id = str(RUN_ID)
        scope = scope_factory(run_id=run_id, with_upstream=True)
        upstream = RunLocalUpstreamResultStore(
            run_id=RUN_ID,
            topology=scope.topology,
            topology_ref=TranscriptContentRef(
                run_id=RUN_ID,
                item_id=UUID("20000000-0000-0000-0000-000000000001"),
            ),
            result_reader=FakeResultReader(),
        )
        audit = MemoryEvidenceStore()
        prepared = []
        runtime = executor(
            audit,
            producer_statuses={
                "get_upstream_results": EvidenceProducerStatus.READY,
            },
            upstream_port=upstream,
            scope_preparer=prepared.append,
        )
        invalid = tool_request(
            scope,
            "get_upstream_results",
            "call-invalid",
        ).model_copy(update={"window_ids": ("window-outside",)})

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(scope, invalid)

        assert prepared == []
        assert audit.calls == []
        assert audit.results == []

        failed = await runtime.execute(
            scope,
            tool_request(scope, "get_upstream_results", "call-valid"),
        )

        assert failed.execution_status is EvidenceToolExecutionStatus.FAILED
        assert failed.safe_error is not None
        assert failed.safe_error.code == "evidence_producer_unavailable"
        assert prepared == [scope]
        assert len(audit.calls) == 1
        assert len(audit.results) == 1

    asyncio.run(exercise())


def test_real_port_success_flows_through_m5_validation_and_collection(
    scope_factory,
    context_factory,
) -> None:
    async def exercise() -> None:
        context = context_factory(
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 1),
            analysis_timezone=ANALYSIS_TIMEZONE,
        )
        scope = scope_factory(
            run_id=str(RUN_ID),
            context=context,
            with_upstream=True,
        )
        session_id = UUID("30000000-0000-0000-0000-000000000200")
        parameter = WaterQualityParameter(
            key=scope.requested_parameters[0],
            name="Dissolved oxygen",
            canonical_unit="mg/L",
            category="chemical",
        )
        result = inference_result(
            "200",
            session_id,
            statuses=(OutcomeStatus.QUANTIFIED,) * len(scope.time_windows),
            time_range=local_day(),
            resolution=scope.time_resolution,
            parameter=parameter,
        )
        outcome = dependency_outcome("200", session_id, result=result)
        assert outcome.result_ref is not None
        result_reader = FakeResultReader()
        result_reader.values[outcome.result_ref.item_id] = resolved_result(result)
        upstream = RunLocalUpstreamResultStore(
            run_id=RUN_ID,
            topology=scope.topology,
            topology_ref=TranscriptContentRef(
                run_id=RUN_ID,
                item_id=UUID("20000000-0000-0000-0000-000000000001"),
            ),
            result_reader=result_reader,
        )
        await upstream.publish(outcome)

        audit = MemoryEvidenceStore()
        audit.seed_session(SessionSummary(
            session_id=session_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="200",
            status=AgentSessionStatus.COMPLETED,
            created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
            updated_at=datetime(2026, 10, 3, 1, tzinfo=timezone.utc),
            result_ref=outcome.result_ref,
            result_summary=outcome.result_summary,
        ))
        audit.seed_scope(
            str(outcome.result_ref.item_id),
            run_id=str(RUN_ID),
            session_id=str(session_id),
        )
        for reference in resolved_result(result).closure_refs:
            audit.seed_scope(
                reference.item_id,
                run_id=str(RUN_ID),
                session_id=str(session_id),
            )
        runtime = executor(
            audit,
            producer_statuses={
                "get_upstream_results": EvidenceProducerStatus.READY,
            },
            upstream_port=upstream,
        )

        execution = await runtime.execute(
            scope,
            tool_request(scope, "get_upstream_results", "call-success"),
        )

        assert execution.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert execution.safe_error is None
        assert execution.fragment_refs
        assert len(audit.calls) == 1
        assert len(audit.results) == 1

    asyncio.run(exercise())
