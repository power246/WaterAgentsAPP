"""Numeric dependencies and original measurements survive persisted reads."""

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

from app.application.analysis_runs.models import AgentRole
from app.application.analysis_runs.workflow import WorkflowContext
from app.application.evidence_tools.measurement_provenance import (
    derive_measured_anchors,
)
from app.application.evidence_tools.numeric_dependency import (
    NumericDependencyExecutor,
    NumericDependencyPlanner,
    prepare_numeric_scope,
)
from app.application.evidence_tools.numeric_estimation import NumericEstimateResult
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
)
from app.application.evidence_tools.transcript_store import EvidenceTranscriptStore
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionManifest,
    CollectionPageManifest,
    CollectionStatus,
    CoverageStatus,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    NumericDependencyPlan,
    NumericDerivedPayload,
    NumericPlanTask,
    ParameterKey,
    Provenance,
    WindowCompleteness,
)
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.store import PostgresTranscriptStore
from scripts.test_database import temporary_migrated_schema
from tests.application.evidence_tools.conftest import make_scope
from tests.application.evidence_tools.test_collection_service import observation
from tests.m4_support import create_request_bytes, create_test_app


class _HoldingWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[WorkflowContext] = asyncio.Queue()
        self.release = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.calls.put_nowait(context)
        await self.release.wait()
        context.cancellation.checkpoint()


class _NoLiveReads:
    async def get_live_read(self, run_id):
        raise AssertionError(f"numeric evidence used live run {run_id}")


class _SelectedOnlyPostgresStore(PostgresTranscriptStore):
    def __init__(self, engine, *, operation_timeout_seconds: float) -> None:
        super().__init__(engine, operation_timeout_seconds=operation_timeout_seconds)
        self.selected_ids: list[tuple[UUID, ...]] = []

    async def read_run(self, run_id):
        raise AssertionError(f"numeric evidence scanned full run {run_id}")

    async def read_run_items(self, run_id, item_ids):
        self.selected_ids.append(tuple(item_ids))
        return await super().read_run_items(run_id, item_ids)


class _CapturingEvidenceStore(EvidenceTranscriptStore):
    def __init__(self, *, intermediate: ParameterKey, **kwargs) -> None:
        super().__init__(**kwargs)
        self.intermediate = intermediate
        self.downstream_reads = []

    async def read_fragments(self, scope, references):
        fragments = await super().read_fragments(scope, references)
        if len(references) == 2 and any(
            fragment.parameter_refs == (self.intermediate,)
            and isinstance(fragment.payload, NumericDerivedPayload)
            for fragment in fragments
        ):
            self.downstream_reads.append((references, fragments))
        return fragments


async def _exercise(settings: AppSettings) -> None:
    workflow = _HoldingWorkflow()
    app = create_test_app(settings, workflow=workflow, shutdown_grace_period=0)
    engine = create_database_engine(settings)
    try:
        async with app.router.lifespan_context(app):
            coordinator = app.state.run_coordinator
            created = await coordinator.create_run(create_request_bytes("numeric DAG"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="123",
            )
            await coordinator.mark_agent_session_running(
                created.run_id, session.session_id
            )

            scope = make_scope(
                run_id=str(created.run_id), session_id=str(session.session_id)
            )
            a, b, c, d = (
                ParameterKey(code_system="test", code=code)
                for code in ("a", "b", "c", "d")
            )
            target = scope.requested_parameters[0]
            modes = tuple(sorted({
                window.window_kind.value for window in scope.time_windows
                if window.window_kind is not None
            }))
            registry = NumericMethodRegistry((
                NumericMethod(
                    method_id="test_sum_a_b", version=1,
                    output_parameter=c, output_unit="mg/L",
                    inputs=(NumericMethodInput(a, "mg/L"),
                            NumericMethodInput(b, "mg/L")),
                    calculate=lambda values, _ranges, _target: sum(values),
                    confidence_cap=0.6, allowed_modes=modes,
                ),
                NumericMethod(
                    method_id="test_subtract_d_from_c", version=1,
                    output_parameter=target, output_unit="mg/L",
                    inputs=(NumericMethodInput(c, "mg/L"),
                            NumericMethodInput(d, "mg/L")),
                    calculate=lambda values, _ranges, _target: values[0] - values[1],
                    confidence_cap=0.5, allowed_modes=modes,
                ),
            ))
            scope = prepare_numeric_scope(scope, registry)
            assert set(scope.intermediate_parameters) == {a, b, c, d}

            persisted = _SelectedOnlyPostgresStore(
                engine, operation_timeout_seconds=settings.db_operation_timeout_seconds
            )
            reader = TranscriptReader(
                _NoLiveReads(), persisted, create_production_registry(),
                error_id_factory=uuid4,
                inline_content_bytes=1_000_000, preview_bytes=64,
            )
            evidence = _CapturingEvidenceStore(
                operations=coordinator, reader=reader,
                capability_registry=load_parameter_capability_registry(),
                read_view=ReadView.HISTORY, intermediate=c,
            )
            window = scope.time_windows[0]
            operation_id = str(uuid4())
            parameters = (a, b, d, target)
            records = tuple(
                observation(scope, index, parameter=parameter, page="seed-page")
                for index, parameter in enumerate(parameters)
            )
            records = (*records[:3], records[3].model_copy(update={
                "location": records[3].location.model_copy(update={
                    "distance_to_target_km": 4.25,
                }),
            }))
            manifest = CollectionManifest(
                manifest_id="numeric-seeds", operation_id=operation_id,
                provider="test", channel="continuous",
                collection_status=CollectionStatus.AVAILABLE,
                request_range=window.time_range,
                requested_series=("test-seeds",),
                page_or_chunk_manifests=(CollectionPageManifest(
                    page_identity="seed-page", record_count=4,
                    response_bytes=1, page_complete=True,
                ),),
                completed_ranges=(window.time_range,),
                affected_window_ids=(window.window_id,),
                window_completeness=(WindowCompleteness(
                    window_id=window.window_id, complete=True,
                    status=CollectionStatus.AVAILABLE,
                ),),
                counts=CollectionCounts(
                    requests=1, pages=1, records=4, response_bytes=1,
                ),
                completion_proof=CollectionCompletionProof(
                    contract="single_response", complete=True,
                    terminal_identity="seed-page", digest="sha256:test-seeds",
                ),
            )
            source = await evidence.write_collection(
                scope, operation_id=operation_id,
                manifests=(manifest,), records=records,
            )
            record_fragment_refs = []
            for parameter, record, record_ref in zip(
                parameters, records, source.source_record_refs, strict=True
            ):
                fragment = EvidenceFragment(
                    evidence_id=f"test-seed-{parameter.code}",
                    source="test", collection_status=CollectionStatus.AVAILABLE,
                    target_comid=scope.agent_comid,
                    location=EvidenceLocation(
                        point=None, comid=scope.agent_comid,
                        station_id="USGS-123",
                        distance_to_target_km=record.location.distance_to_target_km,
                    ),
                    time_range=window.time_range,
                    parameter_refs=(parameter,), data_type="water_observation",
                    applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
                    series_identity=f"test-series-{parameter.code}",
                    quality_class=EvidenceQualityClass.VALID,
                    coverage=EvidenceCoverage(
                        status=CoverageStatus.PARTIAL, observed_count=1,
                        covered_duration_seconds=0,
                        window_duration_seconds=int(
                            (window.time_range.end-window.time_range.start).total_seconds()
                        ),
                    ),
                    payload=DirectRecordPayload(
                        record_ref=record_ref, displayed_value=record.value,
                        displayed_unit=record.raw_unit,
                    ),
                    source_record_refs=(record_ref,),
                    provenance=Provenance(operation_id=operation_id),
                    result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
                )
                record_fragment_refs.append(await evidence.write_fragment(
                    scope, operation_id=operation_id, fragment=fragment
                ))
            seed_refs = record_fragment_refs[:3]
            anchor_ref = record_fragment_refs[3]

            plan = await NumericDependencyPlanner(
                registry=registry, reader=evidence
            ).plan(scope, tuple(seed_refs))
            assert len(plan.tasks) == 2
            outcome = await NumericDependencyExecutor(
                registry=registry, reader=evidence, writer=evidence,
                limiter=asyncio.Semaphore(2),
            ).run(scope, plan)
            assert not outcome.blocked_task_ids and not outcome.failed_task_ids
            assert outcome.plan_ref is not None
            assert len(outcome.fragment_refs) == 2

            derived = await evidence.read_fragments(scope, outcome.fragment_refs)
            by_parameter = {fragment.parameter_refs[0]: (reference, fragment)
                            for reference, fragment in zip(
                                outcome.fragment_refs, derived, strict=True
                            )}
            intermediate_ref, intermediate = by_parameter[c]
            target_ref, target_fragment = by_parameter[target]
            assert isinstance(intermediate.payload, NumericDerivedPayload)
            assert isinstance(target_fragment.payload, NumericDerivedPayload)
            assert Decimal(intermediate.payload.value) == Decimal(15)
            assert intermediate.payload.input_evidence_refs == tuple(seed_refs[:2])
            assert Decimal(target_fragment.payload.value) == Decimal(6)
            assert target_fragment.payload.input_evidence_refs == (
                intermediate_ref, seed_refs[2]
            )
            assert intermediate.payload.plan_item_ref == outcome.plan_ref
            assert target_fragment.payload.plan_item_ref == outcome.plan_ref
            assert any(
                references == (intermediate_ref, seed_refs[2])
                and fragments[0] == intermediate
                for references, fragments in evidence.downstream_reads
            )
            assert any(set(ids) == {
                UUID(intermediate_ref.item_id), UUID(seed_refs[2].item_id)
            } for ids in persisted.selected_ids)

            model_registry = NumericMethodRegistry((NumericMethod(
                method_id="test_range_estimate", version=1,
                output_parameter=target, output_unit="mg/L", inputs=(),
                calculate=None, confidence_cap=1, kind="model_estimate",
                allowed_modes=modes,
            ),))
            model_plan = await NumericDependencyPlanner(
                registry=model_registry, reader=evidence,
            ).plan(scope, tuple(seed_refs), allow_estimation=True)

            async def range_estimator(request):
                assert not request.measured_anchors
                assert len(request.context_refs) == 3
                return NumericEstimateResult(
                    value=None, value_lower="5", value_upper="10",
                    confidence=0.1, rationale="Low-basis range from related readings.",
                    model_provider="test", model_name="fixture",
                    operation_id="range-model-call", prompt_digest="range-prompt",
                )

            model_outcome = await NumericDependencyExecutor(
                registry=model_registry, reader=evidence, writer=evidence,
                limiter=asyncio.Semaphore(1),
            ).run(scope, model_plan, estimator=range_estimator)
            assert not model_outcome.failed_task_ids and not model_outcome.blocked_task_ids
            assert model_outcome.plan_ref is not None
            range_ref = model_outcome.fragment_refs[0]
            range_fragment = await evidence.read_fragment(scope, range_ref)
            assert range_fragment.payload.value is None
            assert (range_fragment.payload.value_lower, range_fragment.payload.value_upper) == ("5", "10")
            assert range_fragment.payload.inference_basis == "related_evidence"

            anchored_registry = NumericMethodRegistry((NumericMethod(
                method_id="test_anchored_estimate", version=1,
                output_parameter=target, output_unit="mg/L", inputs=(),
                calculate=None, confidence_cap=1, kind="model_estimate",
                allowed_modes=modes,
            ),))
            anchored_task = NumericPlanTask(
                task_id="saved-anchor-point", target_comid=scope.agent_comid,
                output_parameter=target, output_unit="mg/L",
                window_id=window.window_id, mode=window.window_kind.value,
                method_id="test_anchored_estimate", method_version=1,
                method_kind="model_estimate", inputs=(),
            )
            anchored_plan = NumericDependencyPlan.create(
                run_id=scope.run_id, session_id=scope.session_id,
                target_comid=scope.agent_comid,
                method_registry_digest=anchored_registry.manifest_digest,
                source_fragment_refs=(anchor_ref,), tasks=(anchored_task,),
            )

            async def anchored_estimator(request):
                assert len(request.measured_anchors) == 1
                anchor = request.measured_anchors[0]
                assert anchor.source_record_refs == (source.source_record_refs[3],)
                assert anchor.origin_comid == scope.agent_comid
                assert anchor.path_comids == (scope.agent_comid,)
                assert anchor.distance_km == 4.25
                return NumericEstimateResult(
                    value="10", value_lower="9", value_upper="11",
                    confidence=0.5, rationale="Estimated from a saved local reading.",
                    model_provider="test", model_name="fixture",
                    operation_id="anchored-model-call", prompt_digest="anchored-prompt",
                )

            anchored_outcome = await NumericDependencyExecutor(
                registry=anchored_registry, reader=evidence, writer=evidence,
                limiter=asyncio.Semaphore(1),
            ).run(scope, anchored_plan, estimator=anchored_estimator)
            assert not anchored_outcome.failed_task_ids
            assert anchored_outcome.plan_ref is not None
            anchored_ref = anchored_outcome.fragment_refs[0]
            anchored_fragment = await evidence.read_fragment(scope, anchored_ref)
            assert anchored_fragment.payload.value == "10"
            assert anchored_fragment.payload.inference_basis == "same_parameter_measurement"
            workflow.release.set()

        # A reader created after the writer has shut down still decodes the
        # exact plan and both numeric values by their selected PostgreSQL IDs.
        restart_engine = create_database_engine(settings)
        try:
            restart_store = _SelectedOnlyPostgresStore(
                restart_engine,
                operation_timeout_seconds=settings.db_operation_timeout_seconds,
            )
            restarted = TranscriptReader(
                _NoLiveReads(), restart_store, create_production_registry(),
                error_id_factory=uuid4,
                inline_content_bytes=1_000_000, preview_bytes=64,
            )
            selected_ids = (
                UUID(outcome.plan_ref.item_id), UUID(intermediate_ref.item_id),
                UUID(target_ref.item_id), UUID(model_outcome.plan_ref.item_id),
                UUID(range_ref.item_id), UUID(anchored_outcome.plan_ref.item_id),
                UUID(anchored_ref.item_id), UUID(anchor_ref.item_id),
                *(UUID(ref.item_id) for ref in seed_refs),
                UUID(source.source_record_refs[3].item_id),
            )
            selected = await restarted.read_registered_items_persisted(
                created.run_id,
                selected_ids,
                ReadView.HISTORY,
            )
            saved = {item.item.item_id: item.body for item in selected}
            assert set(saved) == set(selected_ids)
            assert isinstance(saved[UUID(outcome.plan_ref.item_id)], NumericDependencyPlan)
            assert saved[UUID(outcome.plan_ref.item_id)] == plan
            assert saved[UUID(intermediate_ref.item_id)] == intermediate
            assert saved[UUID(target_ref.item_id)] == target_fragment
            assert saved[UUID(model_outcome.plan_ref.item_id)] == model_plan
            saved_range = saved[UUID(range_ref.item_id)]
            assert saved_range == range_fragment
            assert saved[UUID(anchored_outcome.plan_ref.item_id)] == anchored_plan
            saved_anchored = saved[UUID(anchored_ref.item_id)]
            assert saved_anchored == anchored_fragment
            saved_direct_anchor = saved[UUID(anchor_ref.item_id)]
            assert saved_direct_anchor.location.comid == scope.agent_comid
            saved_seeds = tuple(saved[UUID(ref.item_id)] for ref in seed_refs)
            assert tuple(item.parameter_refs for item in saved_seeds) == ((a,), (b,), (d,))
            anchor = saved_anchored.payload.measured_anchors[0]
            assert anchor.source_record_refs == (source.source_record_refs[3],)
            assert anchor.origin_comid == scope.agent_comid
            assert anchor.path_comids == (scope.agent_comid,)
            assert anchor.distance_km == 4.25
            source_records = saved[UUID(source.source_record_refs[3].item_id)].records
            original = next(record for record in source_records
                            if record.record_id == anchor.source_record_refs[0].record_id)
            assert original == records[3]
            assert original.location.distance_to_target_km == anchor.distance_km
            fragments = {
                item.evidence_id: item
                for item in (saved_anchored, saved_direct_anchor, *saved_seeds)
            }
            assert derive_measured_anchors(
                parameter=target, target_comid=scope.agent_comid,
                evidence_ids=(saved_anchored.evidence_id,), fragments=fragments,
            ) == (anchor,)
            assert saved_range.payload.value is None
            assert saved_range.payload.measured_anchors == ()
            fragments[saved_range.evidence_id] = saved_range
            assert derive_measured_anchors(
                parameter=target, target_comid=scope.agent_comid,
                evidence_ids=(saved_range.evidence_id,), fragments=fragments,
            ) == ()
            assert restart_store.selected_ids == [selected_ids]
        finally:
            await restart_engine.dispose()
    finally:
        workflow.release.set()
        await engine.dispose()


def test_numeric_dependency_reloads_intermediate_anchor_and_range() -> None:
    with temporary_migrated_schema() as settings:
        asyncio.run(_exercise(settings))
