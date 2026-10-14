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
from app.application.analysis_runs.models import RunStatus
from app.application.transcripts.models import StoredTranscriptItem, WorkflowQuiescedFact
from app.application.transcripts.read_models import StoredRunHeader, StoredRunRead
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    AnalysisDetail,
    EvidenceBundle,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceToolExecutionStatus,
    ItemReference,
    OutcomeStatus,
    UpstreamResultPayload,
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
from tests.domain.test_evidence import sample_fragments, valid_bundle


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
        assert first.cell_projection is not None
        assert first.cell_projection.representative_value == result.series[0].points[0].representative_value
        assert first.cell_projection.risk_level == result.series[0].points[0].assessment.risk_level
        assert batch.records[1].cell_projection.trend == result.series[0].points[1].assessment.trend
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

        async def read_registered_run(
            self,
            run_id,
            view,
        ):
            self.calls.append((run_id, view))
            return SimpleNamespace(run=SimpleNamespace(run_id=run_id), items=(
                SimpleNamespace(
                    item=SimpleNamespace(
                        item_id=self.result_item_id,
                        session_id=self.result.session_id,
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
                        session_id=self.result.session_id,
                        content_type="evidence_bundle",
                        content_schema_version=1,
                    ),
                    body=SimpleNamespace(),
                ),
            ))

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
        assert reader.calls[0][1].value == "auto"

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
                    session_id=session_id,
                    content_type="inference_result",
                    content_schema_version=1,
                ),
                body=result,
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=bundle_item_id, session_id=session_id),
                body=bundle,
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=fragment_item_id, session_id=session_id),
                body=SimpleNamespace(),
            ),
            SimpleNamespace(
                item=SimpleNamespace(item_id=source_item_id, session_id=session_id),
                body=SimpleNamespace(),
            ),
        )

        class RegisteredReader:
            async def read_registered_run(
                self,
                run_id,
                view,
            ):
                assert run_id == RUN_ID
                return SimpleNamespace(run=SimpleNamespace(run_id=run_id), items=items)

        resolved = await TranscriptUpstreamInferenceResultReader(
            RegisteredReader()
        ).resolve(outcome)

        assert tuple(reference.item_id for reference in resolved.closure_refs) == (
            str(bundle_item_id),
            str(fragment_item_id),
            str(source_item_id),
        )

    asyncio.run(exercise())


def test_transcript_result_reader_keeps_validated_upstream_refs_out_of_session_closure() -> None:
    async def exercise() -> None:
        session_id = UUID("30000000-0000-0000-0000-000000000200")
        upstream_session_id = UUID("30000000-0000-0000-0000-000000000100")
        result = inference_result("200", session_id)
        outcome = dependency_outcome("200", session_id, result=result)
        assert outcome.result_ref is not None
        bundle_id = UUID(result.analysis_detail.transcript_item_refs[0])
        fragment_id = UUID("70000000-0000-0000-0000-000000000002")
        upstream_result_id = UUID("70000000-0000-0000-0000-000000000003")
        upstream_closure_id = UUID("70000000-0000-0000-0000-000000000004")
        payload = UpstreamResultPayload(
            upstream_session_id=str(upstream_session_id),
            result_ref=ItemReference(item_id=str(upstream_result_id)),
            closure_refs=(ItemReference(item_id=str(upstream_closure_id)),),
        )
        fragment = EvidenceFragment.model_construct(payload=payload)

        def registered(item_id, owner, body):
            return SimpleNamespace(
                item=SimpleNamespace(
                    item_id=item_id,
                    session_id=owner,
                    content_type="inference_result" if item_id == outcome.result_ref.item_id else "evidence_bundle",
                    content_schema_version=1,
                ),
                body=body,
            )

        class RegisteredReader:
            def __init__(self, extra_ref: bool = False) -> None:
                self.extra_ref = extra_ref

            async def read_registered_run(self, run_id, view):
                assert run_id == RUN_ID
                closure = (upstream_result_id, upstream_closure_id)
                if self.extra_ref:
                    closure = (*closure, UUID("70000000-0000-0000-0000-000000000005"))
                extra_items = (
                    (registered(closure[-1], upstream_session_id, object()),)
                    if self.extra_ref else ()
                )
                bundle = EvidenceBundle.model_construct(
                    fragment_refs=(EvidenceFragmentReference(
                        item_id=str(fragment_id), evidence_id="evidence-empty"
                    ),),
                    source_record_item_refs=(),
                    closure_refs=tuple(ItemReference(item_id=str(item)) for item in closure),
                    tool_result_refs=(),
                )
                items = (
                    registered(outcome.result_ref.item_id, session_id, result),
                    registered(bundle_id, session_id, bundle),
                    registered(fragment_id, session_id, fragment),
                    registered(upstream_result_id, upstream_session_id, object()),
                    registered(upstream_closure_id, upstream_session_id, object()),
                    *extra_items,
                )
                return SimpleNamespace(run=SimpleNamespace(run_id=run_id), items=items)

        resolved = await TranscriptUpstreamInferenceResultReader(
            RegisteredReader()
        ).resolve(outcome)
        assert tuple(ref.item_id for ref in resolved.closure_refs) == (
            str(bundle_id), str(fragment_id)
        )
        with pytest.raises(ValueError, match="upstream reference closure"):
            await TranscriptUpstreamInferenceResultReader(
                RegisteredReader(extra_ref=True)
            ).resolve(outcome)

    asyncio.run(exercise())


def test_production_transcript_reader_resolves_downstream_result_with_upstream_refs() -> None:
    async def exercise() -> None:
        upstream_session_id = UUID("30000000-0000-0000-0000-000000000100")
        downstream_session_id = UUID("30000000-0000-0000-0000-000000000200")
        upstream_result_id = UUID("70000000-0000-0000-0000-000000000001")
        upstream_closure_id = UUID("70000000-0000-0000-0000-000000000002")
        downstream_fragment_id = UUID("70000000-0000-0000-0000-000000000003")
        downstream_bundle_id = UUID("70000000-0000-0000-0000-000000000004")
        downstream_result_id = UUID("70000000-0000-0000-0000-000000000005")
        upstream_result = inference_result(
            "100", upstream_session_id, closure_item_id=upstream_closure_id
        )
        downstream_result = inference_result(
            "200", downstream_session_id, closure_item_id=downstream_bundle_id
        )
        outcome = dependency_outcome(
            "200", downstream_session_id, result=downstream_result,
            result_item_id=downstream_result_id,
        )
        upstream_fragment = sample_fragments()[5]
        fragment = EvidenceFragment.model_validate({
            **upstream_fragment.model_dump(mode="python"),
            "evidence_id": "evidence-empty",
            "payload": UpstreamResultPayload(
                upstream_session_id=str(upstream_session_id),
                result_ref=ItemReference(item_id=str(upstream_result_id)),
                closure_refs=(ItemReference(item_id=str(upstream_closure_id)),),
            ),
        })
        base = valid_bundle()
        bundle = EvidenceBundle.model_validate({
            **base.model_dump(mode="python"),
            "context_ref": ItemReference(item_id=str(UUID(int=21))),
            "topology_ref": ItemReference(item_id=str(UUID(int=22))),
            "window_refs": ("w000",),
            "cells": (
                EvidenceCell(
                    parameter=base.requested_parameters[0],
                    window_id="w000",
                    role=EvidenceCellRole.TARGET,
                    allowed_indirect_fragment_refs=(fragment.evidence_id,),
                    status=EvidenceCellStatus.SUPPORTED,
                ),
                EvidenceCell(
                    parameter=base.supporting_parameters[0],
                    window_id="w000",
                    role=EvidenceCellRole.REQUIRED_SUPPORT,
                    status=EvidenceCellStatus.UNSUPPORTED,
                    reasons=("no_usable_evidence",),
                ),
            ),
            "fragment_refs": (EvidenceFragmentReference(
                item_id=str(downstream_fragment_id), evidence_id=fragment.evidence_id
            ),),
            "source_record_item_refs": (),
            "tool_result_refs": (),
            "closure_refs": (
                ItemReference(item_id=str(upstream_result_id)),
                ItemReference(item_id=str(upstream_closure_id)),
            ),
        })
        registry = create_production_registry()

        def stored(item_id, session_id, sequence, content_type, body):
            encoded = registry.encode(content_type, 1, body)
            return StoredTranscriptItem(
                item_id=item_id, run_id=RUN_ID, session_id=session_id,
                transcript_sequence=sequence,
                created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                content_type=content_type, content_schema_version=1,
                inline_body=encoded.safe_body, content_bytes=encoded.content_bytes,
            )

        items = (
            stored(upstream_closure_id, upstream_session_id, 1,
                   "runtime_fact", WorkflowQuiescedFact()),
            stored(upstream_result_id, upstream_session_id, 2,
                   "inference_result", upstream_result),
            stored(downstream_fragment_id, downstream_session_id, 3,
                   "evidence_fragment", fragment),
            stored(downstream_bundle_id, downstream_session_id, 4,
                   "evidence_bundle", bundle),
            stored(downstream_result_id, downstream_session_id, 5,
                   "inference_result", downstream_result),
        )

        class Store:
            async def read_run(self, run_id):
                assert run_id == RUN_ID
                return StoredRunRead(
                    run=StoredRunHeader(
                        run_id=RUN_ID, status=RunStatus.COMPLETED,
                        created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                        updated_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
                        query="Inspect river",
                        request_item_id=UUID(int=10), created_item_id=UUID(int=11),
                    ),
                    items=items,
                )

        class NoLive:
            async def get_live_read(self, run_id):
                assert run_id == RUN_ID
                return None

        reader = TranscriptReader(
            NoLive(), Store(), registry,
            error_id_factory=lambda: UUID(int=12),
            inline_content_bytes=65536, preview_bytes=1024,
        )
        resolved = await TranscriptUpstreamInferenceResultReader(reader).resolve(outcome)
        assert resolved.result == downstream_result
        assert tuple(ref.item_id for ref in resolved.closure_refs) == (
            str(downstream_bundle_id), str(downstream_fragment_id)
        )
        provider = RunLocalUpstreamResultStore(
            run_id=RUN_ID,
            topology=topology("chain"),
            topology_ref=TranscriptContentRef(run_id=RUN_ID, item_id=UUID(int=22)),
            result_reader=TranscriptUpstreamInferenceResultReader(reader),
        )
        await provider.publish(outcome)
        batch = await provider.collect(request(
            agent_comid="300", upstreams=("200",),
            windows=("window-000", "window-001"),
        ))
        assert len(batch.records) == 2
        assert batch.records[0].evidence_closure_refs == resolved.closure_refs
        assert batch.records[0].cell_projection is not None

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
        fragments = tuple(
            audit.fragments[(reference.item_id, reference.evidence_id)]
            for reference in execution.fragment_refs
        )
        assert any(
            getattr(fragment.payload, "cell_projection", None) is not None
            for fragment in fragments
        )
        assert len(audit.calls) == 1
        assert len(audit.results) == 1

    asyncio.run(exercise())
