import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.evidence_tools.bundle_builder import ValidatedBundleInputs
from app.application.evidence_tools.models import EvidenceInspectionPage
from app.application.reasoning import (
    EvidencePlanningLoop,
    PlanningConvergenceReason,
    PlanningLoopError,
    PlanningLoopLimits,
    ReasoningGateDecision,
    validate_waterbody_agent_task,
)
from app.application.reasoning.errors import ReasoningPlanTooLargeError
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyModelPhase,
)
from app.application.reasoning.planning_loop import (
    _bounded_fragment_summary,
    _evidence_summaries,
    _planning_response_schema,
    _project_planning_gates,
    _require_batch_representable_gates,
    _working_context,
    build_reasoning_evidence_summaries,
)
from app.application.reasoning.prompt_builder import (
    EvidencePlanningPromptInput,
    PromptBudgetRemaining,
    PromptBuilder,
    WaterbodyStationKind,
)
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.application.reasoning.sufficiency import ReasoningCellGate
from app.application.reasoning.working_context import BoundedEvidenceSummary
from app.domain import (
    CoverageStatus,
    DataQualityFlag,
    EvidenceApplicability,
    EvidenceCellStatus,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceToolOptions,
    EvidenceToolRequest,
    FacilityRecord,
    GeoPoint,
    ItemReference,
    MeasuredAnchor,
    NumericDerivedPayload,
    OutcomeStatus,
    ParameterKey,
    Provenance,
    ProviderQuality,
    RelationSupportPayload,
    ResultSource,
    RiskLevel,
    SourceRecordReference,
    TemporalEstimatePayload,
    UpstreamResultCellProjection,
    WeatherObservationRecord,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import WaterbodyModelCancelledError
from tests.application.evidence_tools.conftest import (
    MODEL,
    WEATHER_PARAMETER,
    make_context,
    make_graph,
    make_scope,
    make_weather_context,
)
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    complete_batch,
)
from tests.application.evidence_tools.test_acquisition_workflow import (
    _runtime,
    _water_port,
)
from tests.application.reasoning.fakes import (
    DeterministicWaterbodyModelPort,
    ScriptedCancellation,
)
from tests.application.reasoning.test_task_and_skeleton import (
    _catalog,
    _run,
    _session,
    _task,
)
from tests.domain.test_evidence import sample_fragments, valid_source_records

OPS = tuple(
    UUID(f"40000000-0000-4000-8000-{index:012d}")
    for index in range(1, 10)
)


def _event_values(operation_id):
    return {"operation_id": operation_id, "model": MODEL}


def _ready_script(operation_id):
    return (
        StructuredOutputCompleted(
            **_event_values(operation_id),
            output_id=f"output-{operation_id}",
            value='{"decision":"evidence_ready"}',
        ),
        Terminal(
            **_event_values(operation_id),
            provider_response_id=f"response-{operation_id}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        ),
    )


class AbstainingNumericModel(DeterministicWaterbodyModelPort):
    async def stream(self, request):
        if request.batch_id == "guideline-search":
            from tests.guideline_support import search_events
            self.search_requests = [*getattr(self, "search_requests", []), request]
            for event in search_events(request):
                yield event
            return
        if request.phase is not WaterbodyModelPhase.NUMERIC_ESTIMATION:
            async for event in super().stream(request):
                yield event
            return
        self.calls.append(request)
        values = {"operation_id": request.operation_id, "model": request.model}
        yield StructuredOutputCompleted(
            **values,
            output_id="numeric-abstention",
            value=json.dumps({
                "value": None, "value_lower": None, "value_upper": None,
                "confidence": 0.0,
                "rationale": "Available information does not support a useful range.",
            }),
        )
        yield Terminal(
            **values, provider_response_id="numeric-response",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )


def _tool_script(operation_id, *, call_id, window_ids, parameter_keys):
    return (
        ToolCallCompleted(
            **_event_values(operation_id),
            provider_call_id=call_id,
            name="collect_weather_evidence",
            arguments=json.dumps(
                {
                    "window_ids": window_ids,
                    "parameter_keys": [
                        parameter.model_dump(mode="json")
                        for parameter in parameter_keys
                    ],
                    "options": {"include_forecast": False},
                },
                separators=(",", ":"),
            ),
        ),
        Terminal(
            **_event_values(operation_id),
            provider_response_id=f"response-{operation_id}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        ),
    )


async def _fixture(
    model_port,
    *,
    water_mode="full",
    context=None,
    with_station=True,
    with_upstream=False,
    weather_port=None,
    facility_port=None,
    upstream_port=None,
    upstream_ready=False,
    cancel_event=None,
    limits=PlanningLoopLimits(),
    transcript_store=None,
):
    context = context or make_context()
    scope = make_scope(
        run_id="00000000-0000-0000-0000-000000000001",
        session_id="00000000-0000-0000-0000-000000000002",
        context=context,
        with_station=with_station,
        with_upstream=with_upstream,
        cancel_event=cancel_event,
    )
    topology = make_graph(
        with_station=with_station,
        with_upstream=with_upstream,
    )
    store = MemoryEvidenceStore()
    store.seed_session(_session())
    validated = await validate_waterbody_agent_task(
        _task(scope, memory_mode=context.memory_mode),
        run=_run(context),
        context=context,
        topology=topology,
        model_catalog=_catalog(),
        session_reader=store,
    )
    service = _runtime(
        store,
        water_port=(
            _water_port(scope, mode=water_mode) if with_station else None
        ),
        weather_port=weather_port,
        facility_port=facility_port,
        upstream_port=upstream_port,
        upstream_ready=upstream_ready,
    )
    operation_ids = iter(OPS)
    loop = EvidencePlanningLoop(
        acquisition=service,
        capability_registry=load_parameter_capability_registry(),
        prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
        model_port=model_port,
        limits=limits,
        clock=FixedClock(),
        operation_id_factory=lambda: next(operation_ids),
        transcript_store=transcript_store,
    )
    return scope, store, loop, validated


def test_station_backed_empty_attempts_estimation_before_returning_insufficient():
    async def exercise():
        model = AbstainingNumericModel(_ready_script(OPS[0]))
        _, store, loop, validated = await _fixture(model, water_mode="empty")
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.counts.planning_model_calls == 1
        assert outcome.counts.numeric_estimation_calls > 0
        assert len(model.calls) == 1 + outcome.counts.numeric_estimation_calls
        assert outcome.counts.numeric_failure_codes == ()
        assert outcome.counts.final_reasoning_eligible_cells == 0
        assert outcome.counts.reasoning_batch_calls == 0
        assert outcome.counts.synthesis_calls == 0
        assert outcome.counts.convergence_reason is (
            PlanningConvergenceReason.EVIDENCE_READY
        )
        assert len(store.bundles) == 2

    asyncio.run(exercise())


def test_stationless_planning_context_contains_saved_topology_gap() -> None:
    async def exercise():
        model = DeterministicWaterbodyModelPort(_ready_script(OPS[0]))
        scope, _, loop, task = await _fixture(model, with_station=False)
        handoff = await loop._acquisition.start(scope)
        context = await _working_context(
            task, handoff, SimpleNamespace(gates=())
        )

        topology_facts = tuple(
            fact for fact in context.facts
            if fact.fact_id.startswith("topology_gap:")
        )
        assert len(topology_facts) == 2
        assert all(scope.topology_ref.item_id in fact.summary for fact in topology_facts)
        assert all("not a zero reading" in fact.summary for fact in topology_facts)
        assert model.calls == []

    asyncio.run(exercise())


def test_full_and_stationless_inputs_still_require_an_explicit_planning_turn():
    async def exercise():
        full_model = DeterministicWaterbodyModelPort(_ready_script(OPS[0]))
        _, _, full_loop, full_validated = await _fixture(full_model)
        full = await full_loop.run(
            full_validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert full.counts.planning_model_calls == 1
        planning_request = full_model.calls[0]
        planning_schema = json.loads(planning_request.response_schema.schema_text)
        prompt_schema = json.loads(planning_request.messages[-1].text)["response_schema"]
        assert prompt_schema == {
            "name": "evidence_planning_decision",
            "schema": planning_schema,
        }
        assert [
            branch["properties"]["decision"]["const"]
            for branch in planning_schema["oneOf"]
        ] == ["evidence_ready", "evidence_rules_proposed"]
        assert all(
            branch["additionalProperties"] is False
            for branch in planning_schema["oneOf"]
        )
        assert full.counts.final_reasoning_eligible_cells == len(
            full.sealed_input.gates
        )

        stationless_model = DeterministicWaterbodyModelPort(
            _ready_script(OPS[0])
        )
        _, stationless_store, stationless_loop, stationless_validated = (
            await _fixture(
                stationless_model,
                with_station=False,
                with_upstream=True,
            )
        )
        stationless = await stationless_loop.run(
            stationless_validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert stationless.counts.planning_model_calls == 1
        assert all(
            call[1].tool_name != "collect_water_observations"
            for call in stationless_store.calls
        )
        assert stationless.counts.preflight_tool_calls == 1

    asyncio.run(exercise())


def test_planning_rejects_a_reasoning_report_without_a_decision():
    async def exercise():
        model = DeterministicWaterbodyModelPort(
            (
                StructuredOutputCompleted(
                    **_event_values(OPS[0]),
                    output_id="wrong-phase-report",
                    value=json.dumps(
                        {
                            "phase": "evidence_planning",
                            "scope": {"waterbody_comid": "1777616"},
                            "plan": {"evidence_ready": True},
                            "cell_assessments": [],
                            "attribution_notes": [],
                            "integrity": {},
                        }
                    ),
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="wrong-phase-report",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )
        )
        _, _, loop, validated = await _fixture(model)
        with pytest.raises(PlanningLoopError, match="invalid_planning_response"):
            await loop.run(
                validated,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )
        assert len(model.calls) == 1

    asyncio.run(exercise())


def test_inspection_page_is_visible_in_the_next_model_request():
    async def exercise():
        model = DeterministicWaterbodyModelPort()
        scope, _, loop, task = await _fixture(model, water_mode="full")
        handoff = await loop._acquisition.start(scope)
        source_item_id = handoff.source_record_allowlist[0]
        model._scripts = (
            (
                ToolCallCompleted(
                    **_event_values(OPS[0]),
                    provider_call_id="inspect-page-1",
                    name="inspect_evidence_records",
                    arguments=json.dumps({
                        "window_ids": [scope.time_windows[0].window_id],
                        "parameter_keys": [
                            scope.requested_parameters[0].model_dump(mode="json")
                        ],
                        "options": {
                            "source_item_id": source_item_id,
                            "page_offset": 0,
                            "record_limit": 8,
                        },
                    }),
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="inspection-response",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            ),
            _ready_script(OPS[1]),
        )

        await loop.run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
            initial_handoff=handoff,
        )

        page = handoff.executor.inspection_page(scope.run_id, "inspect-page-1")
        assert page is not None and page.records
        body = json.loads(model.calls[1].messages[-1].text)
        summaries = [json.loads(item["summary"]) for item in body["evidence_summaries"]]
        assert any(
            summary.get("inspections", [{}])[0].get("total_records")
            == page.total_records
            and any(record.get("value") == "7" for record in summary["records"])
            for summary in summaries
        )

    asyncio.run(exercise())


def test_large_inspection_page_is_not_repeated_across_shared_source_fragments():
    async def exercise():
        scope, _, loop, task = await _fixture(DeterministicWaterbodyModelPort())
        handoff = await loop._acquisition.start(scope)
        template_record = valid_source_records()[0]
        template_fragment = sample_fragments()[0]
        records = tuple(
            template_record.model_copy(update={
                "record_id": f"provider-record-{index:03d}",
                "value": f"{7 + index / 100:.2f}",
                "provenance": template_record.provenance.model_copy(update={
                    "provider_record_id": f"provider-record-{index:03d}",
                }),
            })
            for index in range(96)
        )
        references = []
        fragments = []
        for index in range(194):
            record = records[index % len(records)]
            source_ref = SourceRecordReference(
                item_id="item-source", record_id=record.record_id,
            )
            fragment = template_fragment.model_copy(update={
                "evidence_id": f"evidence-{index:03d}-{'a' * 56}",
                "payload": template_fragment.payload.model_copy(update={
                    "record_ref": source_ref,
                    "displayed_value": record.value,
                }),
                "source_record_refs": (source_ref,),
            })
            reference = EvidenceFragmentReference(
                item_id="item-fragment", evidence_id=fragment.evidence_id,
            )
            references.append(reference)
            fragments.append((reference, fragment))
        handoff = replace(
            handoff,
            validated_inputs=ValidatedBundleInputs(
                fragments=tuple(fragments),
                source_records=(("item-source", records),),
            ),
            fragment_allowlist=tuple(references),
            source_record_allowlist=("item-source",),
        )
        handoff.executor._inspection_pages[
            (scope.run_id, "inspect-large-page")
        ] = EvidenceInspectionPage(
            source_item_id="item-source",
            offset=0,
            limit=96,
            total_records=96,
            records=records,
            record_refs=tuple(
                SourceRecordReference(
                    item_id="item-source", record_id=record.record_id,
                )
                for record in records
            ),
            next_offset=None,
        )
        gate = ReasoningCellGate(
            cell_id="cell-large",
            parameter=scope.requested_parameters[0],
            window_id=scope.time_windows[0].window_id,
            target_status=EvidenceCellStatus.SUPPORTED,
            discharge_status=EvidenceCellStatus.SUPPORTED,
            decision=ReasoningGateDecision.SUPPORTED,
            allowed_evidence_refs=tuple(
                reference.evidence_id for reference in references
            ),
            contextual_refs=(),
            collection_fact_refs=(),
            blocker_refs=(),
            reasons=(),
        )
        context = await _working_context(
            task, handoff, SimpleNamespace(gates=(gate,)),
        )
        summaries = context.evidence_summaries
        assert len(summaries) == 64
        assert _project_planning_gates((gate,), summaries)[0].allowed_evidence_refs == (
            gate.allowed_evidence_refs
        )
        coverage = next(
            fact for fact in context.facts if fact.fact_id == "coverage:cell-large"
        )
        assert "130 allowed evidence summaries" in coverage.summary
        assert sum('"inspections"' in item.summary for item in summaries) == 1
        assert any('"value":"7.00"' in item.summary for item in summaries)
        compact_gate = gate.model_copy(update={
            "allowed_evidence_refs": (references[-1].evidence_id,),
        })
        compact_context = await _working_context(
            task, handoff, SimpleNamespace(gates=(compact_gate,)),
        )
        assert references[-1].evidence_id in {
            item.evidence_ref for item in compact_context.evidence_summaries
        }
        all_summaries = build_reasoning_evidence_summaries(handoff)
        assert len(all_summaries) == 194
        assert sum('"inspections"' in item.summary for item in all_summaries) == 1
        assert _evidence_summaries(
            handoff, tuple(references[:64]),
        ) == summaries

    asyncio.run(exercise())


def test_large_contextual_gate_projects_only_visible_refs_into_model_prompt():
    async def exercise():
        scope, _, loop, task = await _fixture(DeterministicWaterbodyModelPort())
        handoff = await loop._acquisition.start(scope)
        template = sample_fragments()[0]
        contextual_ids = tuple(
            f"context-{index:04d}-{'c' * 64}" for index in range(860)
        )
        fact_ids = tuple(
            f"fact-{index:04d}-{'f' * 64}" for index in range(6)
        )
        references = tuple(
            EvidenceFragmentReference(
                item_id="large-fragments", evidence_id=evidence_id,
            )
            for evidence_id in (*contextual_ids, *fact_ids)
        )
        fragments = tuple(
            (
                reference,
                template.model_copy(update={
                    "evidence_id": reference.evidence_id,
                    "source_record_refs": (),
                }),
            )
            for reference in references
        )
        handoff = replace(
            handoff,
            validated_inputs=ValidatedBundleInputs(
                fragments=fragments,
                source_records=(),
            ),
            fragment_allowlist=references,
            source_record_allowlist=(),
            block_facts=("evidence_provider_response_too_large",),
        )
        sealed_gate = ReasoningCellGate(
            cell_id="cell-large-contextual",
            parameter=scope.requested_parameters[0],
            window_id=scope.time_windows[0].window_id,
            target_status=EvidenceCellStatus.BLOCKED,
            discharge_status=EvidenceCellStatus.BLOCKED,
            decision=ReasoningGateDecision.BLOCKED,
            allowed_evidence_refs=(),
            contextual_refs=contextual_ids,
            collection_fact_refs=fact_ids,
            blocker_refs=fact_ids,
            reasons=("evidence_provider_response_too_large",),
        )
        context = await _working_context(
            task, handoff, SimpleNamespace(gates=(sealed_gate,)),
        )
        prompt_gate = _project_planning_gates(
            (sealed_gate,), context.evidence_summaries,
        )[0]
        visible = {summary.evidence_ref for summary in context.evidence_summaries}
        assert len(sealed_gate.contextual_refs) == 860
        assert len(prompt_gate.contextual_refs) <= 64
        assert set(prompt_gate.contextual_refs) <= visible
        assert prompt_gate.collection_fact_refs == fact_ids
        assert prompt_gate.blocker_refs == fact_ids
        assert set(prompt_gate.contextual_refs) | set(fact_ids) <= set(
            context.reference_allowlist
        )
        assert all(not fact.refs for fact in context.facts)
        coverage = next(
            fact for fact in context.facts
            if fact.fact_id == "coverage:cell-large-contextual"
        )
        assert (
            f"{860 - len(prompt_gate.contextual_refs)} contextual"
            in coverage.summary
        )
        assert "0 allowed evidence summaries" in coverage.summary

        request = PromptBuilder(
            DEFAULT_REASONING_PROMPT_POLICY
        ).build_evidence_planning(EvidencePlanningPromptInput(
            operation_id=OPS[0],
            model=MODEL,
            deadline=FixedClock().now() + timedelta(minutes=5),
            context=context,
            response_schema=_planning_response_schema(),
            station_kind=WaterbodyStationKind.STATIONLESS,
            gates=(prompt_gate,),
            budget=PromptBudgetRemaining(
                collection_calls=1,
                transform_calls=1,
                planning_turns=1,
                calls_this_turn=1,
            ),
            authorized_tools=(),
        ))
        body = json.loads(request.messages[-1].text)
        assert set(body["gates"][0]["contextual_refs"]) <= {
            summary["evidence_ref"] for summary in body["evidence_summaries"]
        }
        assert len(body["gates"][0]["contextual_refs"]) < 860

    asyncio.run(exercise())


def test_context_projection_reduces_optional_summaries_when_bytes_are_full():
    async def exercise():
        scope, _, loop, task = await _fixture(DeterministicWaterbodyModelPort())
        handoff = await loop._acquisition.start(scope)
        template = sample_fragments()[0]
        relation_ids = tuple(f"a-relation-{index:03d}" for index in range(64))
        dependency_ids = tuple(
            f"z-dependency-{index:03d}-{'d' * 700}" for index in range(64)
        )
        fragments = []
        for index, (relation_id, dependency_id) in enumerate(
            zip(relation_ids, dependency_ids, strict=True)
        ):
            dependency_ref = EvidenceFragmentReference(
                item_id="large-fragments", evidence_id=dependency_id,
            )
            payload = RelationSupportPayload(
                rule_id=f"rule-{index:03d}",
                plan_digest="sha256:rules",
                input_evidence_refs=(dependency_ref,),
                rationale="Directional context for the same window.",
            )
            fragments.append((
                EvidenceFragmentReference(
                    item_id="large-fragments", evidence_id=relation_id,
                ),
                template.model_copy(update={
                    "evidence_id": relation_id,
                    "data_type": "relation_support",
                    "relation_id": payload.rule_id,
                    "payload": payload,
                    "source_record_refs": (),
                }),
            ))
            fragments.append((
                dependency_ref,
                template.model_copy(update={
                    "evidence_id": dependency_id,
                    "source_record_refs": (),
                }),
            ))
        handoff = replace(
            handoff,
            validated_inputs=ValidatedBundleInputs(
                fragments=tuple(fragments), source_records=(),
            ),
            fragment_allowlist=tuple(reference for reference, _ in fragments),
            source_record_allowlist=(),
        )
        gate = ReasoningCellGate(
            cell_id="cell-large-dependencies",
            parameter=scope.requested_parameters[0],
            window_id=scope.time_windows[0].window_id,
            target_status=EvidenceCellStatus.UNSUPPORTED,
            discharge_status=EvidenceCellStatus.UNSUPPORTED,
            decision=ReasoningGateDecision.UNSUPPORTED,
            allowed_evidence_refs=(),
            contextual_refs=(*relation_ids, *dependency_ids),
            collection_fact_refs=(),
            blocker_refs=(),
            reasons=("no_usable_evidence",),
        )
        context = await _working_context(
            task, handoff, SimpleNamespace(gates=(gate,)),
        )
        assert 0 < len(context.evidence_summaries) < 64
        prompt_gate = _project_planning_gates(
            (gate,), context.evidence_summaries,
        )[0]
        assert len(prompt_gate.contextual_refs) == len(context.evidence_summaries)
        assert len(gate.contextual_refs) == 128

    asyncio.run(exercise())


def test_collected_facility_identity_reaches_the_model_request():
    async def exercise():
        records = []

        def facility_batch(plan):
            return complete_batch(plan, records)

        model = DeterministicWaterbodyModelPort(_ready_script(OPS[0]))
        scope, _, loop, task = await _fixture(
            model, facility_port=FakeCollectionPort(facility_batch)
        )
        records.append(FacilityRecord(
            provider="epa_echo",
            channel="cwa_npdes_facility",
            source_uri="https://example.test/?api_key=PRIVATE_MARKER",
            retrieved_at=scope.query_envelope.end,
            observed_time=scope.query_envelope.end,
            valid_time_range=None,
            location=EvidenceLocation(
                point=scope.target_node.point,
                comid=None,
                station_id=None,
                provider_location_id="facility-731",
                distance_to_target_km=0,
            ),
            provider_quality=ProviderQuality(approval="official"),
            raw_unit=None,
            provenance=Provenance(
                provider_record_id="facility-731",
                facility_final_distance_km=0,
            ),
            spatially_in_scope=True,
            registry_id="echo",
            facility_id="facility-731",
            permit_id="permit-731",
            program="NPDES",
            status="active",
            public_attributes=(
                ("facility_name", "Clearwater Works"),
                ("facility_type", "industrial"),
            ),
        ))
        handoff = await loop._acquisition.start(scope)
        handoff = await loop._acquisition.execute_optional(
            scope,
            (EvidenceToolRequest(
                tool_call_id="facility-call",
                session_id=scope.session_id,
                tool_name="collect_facility_evidence",
                purpose="Inspect facility context.",
                window_ids=(scope.time_windows[0].window_id,),
                parameter_keys=(scope.requested_parameters[0],),
                options=EvidenceToolOptions(facility_radius_miles=20),
            ),),
            handoff,
        )
        await loop.run(
            task,
            deadline=FixedClock().now() + timedelta(minutes=5),
            initial_handoff=handoff,
        )
        request_text = model.calls[0].messages[-1].text
        assert "Clearwater Works" in request_text
        assert "industrial" in request_text
        assert "facility-731" in request_text
        assert "permit-731" in request_text
        assert "active" in request_text
        assert "PRIVATE_MARKER" not in request_text
        assert "source_uri" not in request_text

    asyncio.run(exercise())


def test_inspected_later_facility_record_precedes_initial_record_preview():
    facility = next(
        record for record in valid_source_records()
        if isinstance(record, FacilityRecord)
    )
    records = tuple(
        facility.model_copy(update={
            "record_id": f"facility-{index}",
            "facility_id": f"facility-{index}",
            "public_attributes": (("facility_name", f"Facility {index}"),),
        })
        for index in range(9)
    )
    page = EvidenceInspectionPage(
        source_item_id="source-facilities",
        offset=8,
        limit=1,
        total_records=9,
        records=(records[-1],),
        record_refs=(),
        next_offset=None,
    )
    summary, truncated = _bounded_fragment_summary(
        sample_fragments()[3], records, (page,)
    )
    projected = json.loads(summary)
    assert projected["records"][0]["facility_id"] == "facility-8"
    assert projected["records"][0]["public_attributes"]["facility_name"] == "Facility 8"
    assert truncated is True


def test_partial_mean_summary_shows_quality_and_inferred_basis():
    aggregate = sample_fragments()[1]
    aggregate = aggregate.model_copy(update={
        "quality_class": EvidenceQualityClass.SUSPICIOUS,
        "quality_flags": (DataQualityFlag.PARTIAL_COVERAGE,),
        "coverage": aggregate.coverage.model_copy(update={
            "status": CoverageStatus.PARTIAL,
        }),
    })
    summary, _ = _bounded_fragment_summary(aggregate, (), ())
    projected = json.loads(summary)

    assert projected["quality_flags"] == ["partial_coverage"]
    assert projected["coverage_status"] == "partial"
    assert projected["result_eligibility"] == "inferred_only"
    assert projected["payload"]["method"] == "mean"


@pytest.mark.parametrize(
    ("anchor_count", "path_waterbody_count"),
    [(1, 3), (6, 24)],
)
def test_upstream_result_summary_bounds_measured_anchor_source_refs(
    anchor_count: int,
    path_waterbody_count: int,
):
    fragment = sample_fragments()[5]
    parameter = ParameterKey(code_system="usgs", code="00400")
    anchors = tuple(
        MeasuredAnchor(
            parameter=parameter,
            source_record_refs=tuple(
                SourceRecordReference(
                    item_id="upstream-source-records",
                    record_id=f"USGS-pH-{anchor_index:02d}-{record_index:04d}-{'r' * 64}",
                )
                for record_index in range(24)
            ),
            origin_comid=f"origin-{anchor_index}",
            station_id=f"USGS-{anchor_index:08d}",
            evidence_time_range=fragment.time_range,
            path_comids=(
                f"origin-{anchor_index}",
                *(
                    f"middle-{anchor_index}-{index:02d}"
                    for index in range(path_waterbody_count - 2)
                ),
                fragment.target_comid,
            ),
            distance_km=float(anchor_index + 1) * 35,
        )
        for anchor_index in reversed(range(anchor_count))
    )
    cell_projection = UpstreamResultCellProjection(
        parameter=parameter,
        window_id="window-000",
        time_range=fragment.time_range,
        outcome_status=OutcomeStatus.QUANTIFIED,
        representative_value="8.2",
        value_lower="7.8",
        value_upper="8.6",
        unit_id="pH",
        trend=None,
        risk_level=RiskLevel.LOW,
        source=ResultSource.PREDICTED,
        confidence=0.42,
        inference_method="model_estimate",
        inference_basis="same_parameter_measurement",
        measured_anchors=anchors,
    )
    fragment = fragment.model_copy(update={
        "parameter_refs": (parameter,),
        "payload": fragment.payload.model_copy(update={
            "cell_projection": cell_projection,
        }),
    })

    summary, truncated = _bounded_fragment_summary(fragment, (), ())
    projected = json.loads(summary)["payload"]["cell_projection"]

    assert len(summary.encode("utf-8")) <= 1500
    assert projected["representative_value"] == "8.2"
    assert projected["value_lower"] == "7.8"
    assert projected["value_upper"] == "8.6"
    assert projected["confidence"] == 0.42
    assert projected["measured_anchor_count"] == anchor_count
    assert projected["measured_anchors"]
    assert len(projected["measured_anchors"]) <= min(anchor_count, 4)
    nearest = projected["measured_anchors"][0]
    assert nearest["origin_comid"] == "origin-0"
    assert nearest["station_id"] == "USGS-00000000"
    assert nearest["distance_km"] == 35
    assert nearest["path_waterbody_count"] == path_waterbody_count
    assert nearest["source_record_count"] == 24
    assert nearest["evidence_time_range"] == fragment.time_range.model_dump(
        mode="json"
    )
    assert "source_record_refs" not in summary
    assert "path_comids" not in summary
    assert len(fragment.payload.cell_projection.measured_anchors) == anchor_count
    assert all(
        len(anchor.source_record_refs) == 24
        and len(anchor.path_comids) == path_waterbody_count
        for anchor in fragment.payload.cell_projection.measured_anchors
    )
    assert truncated is True


@pytest.mark.parametrize("input_count", [1, 8])
def test_numeric_derivation_summary_exposes_saved_method_plan_and_input(
    input_count: int,
):
    method_id = "temporal_gap_fill_linear_v1_usgs_00300"
    fragment = sample_fragments()[0]
    payload = NumericDerivedPayload(
        value="123456789.123456" if input_count == 8 else "7",
        unit="mg/l",
        method_id=method_id,
        method_version=1,
        task_id="numeric:sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64,
        plan_item_ref=ItemReference(item_id="p" * 36),
        method_registry_digest="sha256:test-registry",
        input_evidence_refs=tuple(
            EvidenceFragmentReference(
                item_id=f"input-item-{index}",
                evidence_id=f"evidence:sha256:{index:064x}",
            )
            for index in range(input_count)
        ),
        input_window_ids=("window-000",) * input_count,
        confidence_cap=0.6,
    )
    fragment = EvidenceFragment.model_validate(fragment.model_copy(update={
        "source": "numeric_method",
        "source_record_refs": (),
        "data_type": "numeric_derived",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "relation_id": method_id,
        "quality_flags": (
            tuple(DataQualityFlag)
            if input_count == 8
            else (DataQualityFlag.ESTIMATED,)
        ),
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "payload": payload,
        "derivation": EvidenceDerivation(
            algorithm=method_id,
            algorithm_version=1,
            policy_id=method_id,
            registry_digest=payload.method_registry_digest,
        ),
    }).model_dump())

    summary, _ = _bounded_fragment_summary(fragment, (), ())
    projected = json.loads(summary)

    assert len(summary.encode("utf-8")) <= 1500
    assert projected["type"] == "numeric_derived"
    assert projected["flags"] == ",".join(
        flag.value for flag in fragment.quality_flags
    )
    assert projected["value"] == payload.value
    assert projected["unit"] == "mg/l"
    assert projected["method_id"] == method_id
    assert projected["version"] == 1
    assert projected["task_id"] == payload.task_id
    assert projected["plan_digest"] == payload.plan_digest
    assert projected["plan_ref"] == "p" * 36
    assert projected["inputs"] == [
        [reference.evidence_id, window_id]
        for reference, window_id in zip(
            payload.input_evidence_refs, payload.input_window_ids, strict=True
        )
    ]
    assert projected["confidence_cap"] == 0.6


@pytest.mark.parametrize("extra_input_characters", [0, 220])
def test_model_estimate_summary_fits_working_context_with_long_paths_and_inputs(
    extra_input_characters: int,
) -> None:
    fragment = sample_fragments()[0]
    input_refs = tuple(
        EvidenceFragmentReference(
            item_id=f"input-item-{index}",
            evidence_id=(
                f"evidence:sha256:{index:064x}" + "x" * extra_input_characters
            ),
        )
        for index in range(8)
    )
    anchors = tuple(
        MeasuredAnchor(
            parameter=fragment.parameter_refs[0],
            source_record_refs=(SourceRecordReference(
                item_id="original-records", record_id=f"observation-{index}",
            ),),
            origin_comid=f"origin-{index}",
            station_id=f"USGS-{index:08d}",
            evidence_time_range=fragment.time_range,
            path_comids=(
                f"origin-{index}",
                *(f"middle-{index}-{step:02d}" for step in range(32)),
                fragment.target_comid,
            ),
            distance_km=float((index + 1) * 10),
            origin_point=(
                GeoPoint(latitude=39.0, longitude=-77.0) if index == 0 else None
            ),
            distance_basis=(
                "mapped_channel_estimate" if index == 0 else None
            ),
        )
        for index in reversed(range(4))
    )
    method_id = "model_estimate_usgs_00400"
    payload = NumericDerivedPayload(
        value="8.1", value_lower="7.8", value_upper="8.4",
        method_kind="model_estimate", inference_basis="same_parameter_measurement",
        measured_anchors=anchors, rationale="Measured pH supports a local estimate. " * 10,
        model_provider="openai", model_name="deepseek-flash",
        model_operation_id="numeric-estimate-1", prompt_digest="sha256:prompt",
        unit="pH", method_id=method_id, method_version=1,
        task_id="numeric:sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64,
        plan_item_ref=ItemReference(item_id="numeric-plan-item"),
        method_registry_digest="sha256:test-registry",
        input_evidence_refs=input_refs,
        input_window_ids=("window-000",) * len(input_refs),
        confidence_cap=0.42,
    )
    fragment = EvidenceFragment.model_validate(fragment.model_copy(update={
        "source": "numeric_method",
        "source_record_refs": (),
        "data_type": "numeric_derived",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "relation_id": method_id,
        "quality_class": EvidenceQualityClass.SUSPICIOUS,
        "quality_flags": (DataQualityFlag.ESTIMATED,),
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "payload": payload,
        "derivation": EvidenceDerivation(
            algorithm=method_id, algorithm_version=1, policy_id=method_id,
            registry_digest=payload.method_registry_digest,
        ),
    }).model_dump())

    summary, truncated = _bounded_fragment_summary(fragment, (), ())
    projected = json.loads(summary)
    bounded = BoundedEvidenceSummary(
        evidence_ref=fragment.evidence_id, summary=summary,
        dependency_evidence_refs=tuple(ref.evidence_id for ref in input_refs),
        truncated=truncated,
    )

    assert len(summary.encode("utf-8")) <= 2048
    assert bounded.truncated is True
    assert (projected["value"], projected["value_lower"], projected["value_upper"]) == (
        "8.1", "7.8", "8.4",
    )
    assert projected["inference_basis"] == "same_parameter_measurement"
    assert projected["rationale"] == payload.rationale[:240]
    assert projected["anchor_count"] == 4
    assert projected["measured_anchors"][0] == {
        "origin_comid": "origin-0", "station_id": "USGS-00000000",
            "distance_km": 10.0,
            "distance_basis": "mapped_channel_estimate",
            "path_waterbody_count": 34,
    }
    assert "path_comids" not in summary
    assert projected["input_count"] == 8
    assert projected["inputs"] == [
        [ref.evidence_id, "window-000"]
        for ref in input_refs[:len(projected["inputs"])]
    ]
    if extra_input_characters == 0:
        assert len(projected["inputs"]) == 8
    else:
        assert len(projected["inputs"]) < 8
    assert bounded.dependency_evidence_refs == tuple(sorted(
        ref.evidence_id for ref in input_refs
    ))


def test_planned_temporal_summary_exposes_plan_and_input() -> None:
    payload = TemporalEstimatePayload(
        value="7",
        unit="mg/l",
        method="nearest_hold",
        input_evidence_refs=(
            EvidenceFragmentReference(
                item_id="anchor-item", evidence_id="anchor-evidence"
            ),
        ),
        anchor_window_ids=("window-000",),
        anchor_distance_seconds=(21600,),
        plan_item_ref=ItemReference(item_id="temporal-plan-item"),
        task_id="numeric:sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64,
        method_registry_digest="sha256:" + "c" * 64,
    )
    fragment = sample_fragments()[0].model_copy(update={"payload": payload})
    summary, truncated = _bounded_fragment_summary(fragment, (), ())
    projected = json.loads(summary)["payload"]

    assert not truncated
    assert len(summary.encode("utf-8")) <= 1500
    assert projected["value"] == "7"
    assert projected["plan_item_ref"] == "temporal-plan-item"
    assert projected["task_id"] == payload.task_id
    assert projected["plan_digest"] == payload.plan_digest
    assert projected["method_registry_digest"] == payload.method_registry_digest
    assert projected["input_evidence_refs"] == ["anchor-evidence"]


@pytest.mark.parametrize("input_ref_size", [32, 700])
def test_relation_support_projection_bounds_long_rationale_and_preserves_refs(
    input_ref_size: int,
):
    rationale = "Upstream pH supports this same-window directional relation. " * 24
    rule_id = "runtime-relation-rule"
    input_ref = "upstream-" + "e" * input_ref_size
    payload = RelationSupportPayload(
        rule_id=rule_id,
        plan_digest="sha256:runtime-rule-plan",
        input_evidence_refs=(
            EvidenceFragmentReference(item_id="upstream-item", evidence_id=input_ref),
        ),
        rationale=rationale,
    )
    original_rationale = payload.rationale
    fragment = sample_fragments()[0].model_copy(update={
        "data_type": "relation_support",
        "relation_id": rule_id,
        "payload": payload,
        "source_record_refs": (),
    })

    summary, truncated = _bounded_fragment_summary(fragment, (), ())
    projected = json.loads(summary)

    assert len(summary.encode("utf-8")) <= 1500
    assert truncated is True
    assert projected["relation_id"] == rule_id
    assert projected["payload"]["input_evidence_refs"] == [input_ref]
    assert projected["payload"]["rationale"]
    assert rationale.startswith(projected["payload"]["rationale"])
    assert len(projected["payload"]["rationale"]) <= 320
    if input_ref_size == 700:
        assert len(projected["payload"]["rationale"]) < 320
    assert payload.rationale == original_rationale


def test_relation_support_projection_still_rejects_oversized_required_refs():
    payload = RelationSupportPayload(
        rule_id="runtime-relation-rule",
        plan_digest="sha256:runtime-rule-plan",
        input_evidence_refs=(
            EvidenceFragmentReference(
                item_id="upstream-item", evidence_id="upstream-" + "e" * 2000
            ),
        ),
        rationale="Directional relation.",
    )
    fragment = sample_fragments()[0].model_copy(update={
        "data_type": "relation_support",
        "relation_id": payload.rule_id,
        "payload": payload,
        "source_record_refs": (),
    })

    with pytest.raises(PlanningLoopError, match="evidence_prompt_projection_too_large"):
        _bounded_fragment_summary(fragment, (), ())


def test_unrepresentable_gate_fails_before_model_reasoning():
    gate = ReasoningCellGate(
        cell_id="cell-too-large",
        parameter=make_context().params_used[0].key,
        window_id="window-000",
        target_status=EvidenceCellStatus.SUPPORTED,
        discharge_status=EvidenceCellStatus.SUPPORTED,
        decision=ReasoningGateDecision.SUPPORTED,
        allowed_evidence_refs=tuple(f"evidence-{index}" for index in range(65)),
        contextual_refs=(),
        collection_fact_refs=(),
        blocker_refs=(),
        reasons=(),
    )
    with pytest.raises(ReasoningPlanTooLargeError):
        _require_batch_representable_gates(SimpleNamespace(gates=(gate,)))

    fit_gate = gate.model_copy(update={
        "allowed_evidence_refs": gate.allowed_evidence_refs[:64],
    })
    _require_batch_representable_gates(SimpleNamespace(gates=(fit_gate,)))


def test_facility_projection_retains_identity_when_optional_labels_are_large():
    facility = next(
        record for record in valid_source_records()
        if isinstance(record, FacilityRecord)
    )
    record = facility.model_copy(update={
        "registry_id": "registry-" + "r" * 72,
        "facility_id": "facility-" + "f" * 71,
        "permit_id": "permit-" + "p" * 73,
        "status": "active",
        "public_attributes": (
            ("facility_name", "Clearwater Works"),
            ("facility_type", "industrial"),
            ("permit_name", "n" * 80),
            ("permit_type", "t" * 80),
            ("city", "c" * 80),
            ("state", "s" * 80),
            ("receiving_waterbody", "w" * 80),
        ),
    })
    summary, truncated = _bounded_fragment_summary(
        sample_fragments()[3], (record,), ()
    )
    projected = json.loads(summary)
    assert truncated is True
    assert projected["records"][0]["facility_id"] == record.facility_id
    assert projected["records"][0]["status"] == "active"
    assert projected["records"][0]["public_attributes"]["facility_name"] == (
        "Clearwater Works"
    )
    assert projected["records"][0]["public_attributes"]["facility_type"] == (
        "industrial"
    )
    assert len(summary.encode("utf-8")) <= 1500


def test_multi_turn_tools_rebuild_bundles_and_exact_duplicates_reuse_provider_work():
    async def exercise():
        context = make_weather_context()
        weather = FakeCollectionPort(lambda plan: complete_batch(plan))
        model = AbstainingNumericModel(
            _tool_script(
                OPS[0],
                call_id="weather-a",
                window_ids=("window-000",),
                parameter_keys=(WEATHER_PARAMETER,),
            ),
            _tool_script(
                OPS[1],
                call_id="weather-b",
                window_ids=("window-000",),
                parameter_keys=(WEATHER_PARAMETER,),
            ),
            _ready_script(OPS[2]),
        )
        _, store, loop, validated = await _fixture(
            model,
            context=context,
            weather_port=weather,
        )
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert len(weather.calls) == 1
        assert outcome.counts.agent_tool_calls == 2
        assert outcome.counts.planning_model_calls == 2
        assert outcome.counts.convergence_reason is PlanningConvergenceReason.NO_PROGRESS
        assert len(outcome.counts.bundle_refs) == 4
        assert len({ref.item_id for ref in outcome.counts.bundle_refs}) == 4
        assert set(store.bundles[0][1].fragment_refs) <= set(
            store.bundles[-1][1].fragment_refs
        )
        assert store.bundles[0][1] != store.bundles[-1][1]

    asyncio.run(exercise())


def test_failed_optional_provider_is_not_offered_or_retried_in_later_turn():
    async def exercise():
        context = make_weather_context()
        weather = FakeCollectionPort(
            lambda plan: complete_batch(plan, complete=False)
        )
        first_call = _tool_script(
            OPS[0],
            call_id="weather-incomplete",
            window_ids=("window-000",),
            parameter_keys=(WEATHER_PARAMETER,),
        )
        model = DeterministicWaterbodyModelPort(
            first_call, _ready_script(OPS[1])
        )
        _, _, loop, validated = await _fixture(
            model, context=context, weather_port=weather
        )
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.counts.planning_model_calls == 2
        assert outcome.counts.agent_tool_calls == 1
        assert len(weather.calls) == 1
        assert "collect_weather_evidence" in {
            tool.name for tool in model.calls[0].tool_descriptors
        }
        weather_schema = json.loads(next(
            tool.input_schema.schema_text
            for tool in model.calls[0].tool_descriptors
            if tool.name == "collect_weather_evidence"
        ))
        assert [
            item["properties"]["code"]["const"]
            for item in weather_schema["properties"]["parameter_keys"]["items"]["oneOf"]
        ] == ["00045"]
        assert "collect_weather_evidence" not in {
            tool.name for tool in model.calls[1].tool_descriptors
        }
        assert "evidence_collection_incomplete" in "\n".join(
            message.text for message in model.calls[1].messages
        )

        repeated = DeterministicWaterbodyModelPort(
            first_call,
            _tool_script(
                OPS[1],
                call_id="weather-repeated",
                window_ids=("window-000",),
                parameter_keys=(WEATHER_PARAMETER,),
            ),
        )
        repeated_weather = FakeCollectionPort(
            lambda plan: complete_batch(plan, complete=False)
        )
        _, _, repeated_loop, repeated_task = await _fixture(
            repeated, context=context, weather_port=repeated_weather
        )
        with pytest.raises(PlanningLoopError, match="invalid_planning_response"):
            await repeated_loop.run(
                repeated_task,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )
        assert len(repeated_weather.calls) == 1

    asyncio.run(exercise())


def test_whitespace_model_text_uses_valid_rationale_for_tool_and_ready_decisions():
    class DecisionRecorder:
        def __init__(self):
            self.rationales = []

        async def write_planning_decision(self, request, **kwargs):
            self.rationales.append(kwargs["rationale"])

    async def exercise():
        recorder = DecisionRecorder()
        context = make_weather_context()
        blank_tool_text = VisibleTextDelta(
            **_event_values(OPS[0]),
            message_id="blank-tool-text",
            text="   \n  ",
        )
        blank_ready_text = VisibleTextDelta(
            **_event_values(OPS[1]),
            message_id="blank-ready-text",
            text="   ",
        )
        model = DeterministicWaterbodyModelPort(
            (
                blank_tool_text,
                *_tool_script(
                    OPS[0],
                    call_id="weather-with-blank-text",
                    window_ids=("window-000",),
                    parameter_keys=(WEATHER_PARAMETER,),
                ),
            ),
            (blank_ready_text, *_ready_script(OPS[1])),
        )
        _, _, loop, validated = await _fixture(
            model,
            context=context,
            weather_port=FakeCollectionPort(lambda plan: complete_batch(plan)),
            transcript_store=recorder,
        )
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert outcome.counts.agent_tool_calls == 1
        assert recorder.rationales == [
            "The model requested authorized evidence tools.",
            "The model reported that evidence planning is complete.",
        ]

    asyncio.run(exercise())


def test_appended_weather_rule_does_not_fabricate_missing_weather():
    async def exercise():
        context = make_context()
        precipitation = load_parameter_capability_registry().exact_matches(
            "precipitation"
        )[0].key
        proposal = {
            "decision": "evidence_rules_proposed",
            "rules": [
                {
                    "output_parameter": context.supporting_params[0].key.model_dump(
                        mode="json"
                    ),
                    "input_parameters": [precipitation.model_dump(mode="json")],
                    "temporal_relation": "same_window",
                    "spatial_relation": "same_waterbody",
                    "maximum_inference_strength": "directional",
                    "required_collection_attempt": False,
                    "rationale": "Rainfall may directionally support discharge.",
                }
            ],
        }
        model = AbstainingNumericModel(
            (
                StructuredOutputCompleted(
                    **_event_values(OPS[0]),
                    output_id="rules-1",
                    value=json.dumps(proposal, separators=(",", ":")),
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-rules",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            ),
            _ready_script(OPS[1]),
        )
        _, store, loop, validated = await _fixture(
            model,
            water_mode="partial",
            context=context,
        )
        initial_bundle_count = len(store.bundles)
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert initial_bundle_count == 0
        assert outcome.counts.rule_proposals == 1
        assert len(outcome.handoff.rule_plan.appended_rules) == 1
        assert outcome.handoff.rule_plan.baseline_rules == (
            context.evidence_rule_plan.baseline_rules
        )
        assert all(
            gate.decision is ReasoningGateDecision.SUPPORTED
            for gate in outcome.sealed_input.gates
        )
        assert outcome.counts.final_reasoning_eligible_cells > 0
        assert len(store.bundles) == 3
        assert not any(
            isinstance(fragment.payload, RelationSupportPayload)
            and fragment.relation_id == outcome.handoff.rule_plan.appended_rules[0].rule_id
            for fragment in store.fragments.values()
        )

    asyncio.run(exercise())


def test_repeated_rule_signature_converges_without_another_bundle_or_model_call():
    async def exercise():
        context = make_context()
        precipitation = load_parameter_capability_registry().exact_matches(
            "precipitation"
        )[0].key
        rule = {
            "output_parameter": context.supporting_params[0].key.model_dump(mode="json"),
            "input_parameters": [precipitation.model_dump(mode="json")],
            "temporal_relation": "same_window",
            "spatial_relation": "same_waterbody",
            "maximum_inference_strength": "directional",
            "required_collection_attempt": False,
            "rationale": "Rainfall may directionally support discharge.",
        }

        def rule_script(operation_id, rationale):
            proposal = {
                "decision": "evidence_rules_proposed",
                "rules": [{**rule, "rationale": rationale}],
            }
            return (
                StructuredOutputCompleted(
                    **_event_values(operation_id),
                    output_id=f"rules-{operation_id}",
                    value=json.dumps(proposal, separators=(",", ":")),
                ),
                Terminal(
                    **_event_values(operation_id),
                    provider_response_id=f"response-{operation_id}",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )

        model = AbstainingNumericModel(
            rule_script(OPS[0], rule["rationale"]),
            rule_script(OPS[1], "Discharge may directionally reflect rainfall."),
            _ready_script(OPS[2]),
        )
        _, store, loop, validated = await _fixture(
            model,
            water_mode="partial",
            context=context,
        )
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert outcome.counts.convergence_reason is PlanningConvergenceReason.NO_PROGRESS
        assert outcome.counts.planning_model_calls == 2
        assert outcome.counts.rule_proposals == 1
        assert len(outcome.handoff.rule_plan.appended_rules) == 1
        assert outcome.handoff.rule_plan.appended_rules[0].rationale == rule["rationale"]
        assert len(store.bundles) == 3
        assert len(model.calls) == 2 + outcome.counts.numeric_estimation_calls

    asyncio.run(exercise())


def test_out_of_scope_rule_proposal_is_typed_invalid_planning_response():
    async def exercise():
        context = make_context()
        precipitation = load_parameter_capability_registry().exact_matches(
            "precipitation"
        )[0].key
        proposal = {
            "decision": "evidence_rules_proposed",
            "rules": [
                {
                    "output_parameter": precipitation.model_dump(mode="json"),
                    "input_parameters": [
                        context.supporting_params[0].key.model_dump(mode="json")
                    ],
                    "temporal_relation": "same_window",
                    "spatial_relation": "same_waterbody",
                    "maximum_inference_strength": "directional",
                    "required_collection_attempt": False,
                    "rationale": "Attempt to add an output outside the saved scope.",
                }
            ],
        }
        model = DeterministicWaterbodyModelPort(
            (
                StructuredOutputCompleted(
                    **_event_values(OPS[0]),
                    output_id="rules-outside-scope",
                    value=json.dumps(proposal, separators=(",", ":")),
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-rules-outside-scope",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )
        )
        _, _, loop, validated = await _fixture(model, context=context)

        with pytest.raises(PlanningLoopError) as raised:
            await loop.run(
                validated,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )

        assert raised.value.code == "invalid_planning_response"

    asyncio.run(exercise())


def test_rule_then_weather_tool_materializes_support_in_later_bundle():
    async def exercise():
        context = make_context()
        precipitation = load_parameter_capability_registry().exact_matches(
            "precipitation"
        )[0].key
        proposal = {
            "decision": "evidence_rules_proposed",
            "rules": [
                {
                    "output_parameter": context.supporting_params[0].key.model_dump(
                        mode="json"
                    ),
                    "input_parameters": [precipitation.model_dump(mode="json")],
                    "temporal_relation": "same_window",
                    "spatial_relation": "same_waterbody",
                    "maximum_inference_strength": "directional",
                    "required_collection_attempt": False,
                    "rationale": "Rainfall may directionally support discharge.",
                }
            ],
        }
        model = AbstainingNumericModel(
            (
                StructuredOutputCompleted(
                    **_event_values(OPS[0]),
                    output_id="rules-weather",
                    value=json.dumps(proposal, separators=(",", ":")),
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-rules-weather",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            ),
            _tool_script(
                OPS[1],
                call_id="weather-after-rule",
                window_ids=("window-000",),
                parameter_keys=(precipitation,),
            ),
            _ready_script(OPS[2]),
        )
        scope_holder = {}

        def weather_batch(plan):
            scope = scope_holder["scope"]
            window = scope.time_windows[0]
            return complete_batch(
                plan,
                (
                    WeatherObservationRecord(
                        provider="nws",
                        channel="observation",
                        source_uri="https://weather.example.test/observation",
                        retrieved_at=scope.query_envelope.end,
                        observed_time=window.time_range.start + timedelta(minutes=5),
                        location=EvidenceLocation(
                            point=scope.target_node.point,
                            comid=None,
                            station_id="NWS-TEST",
                            distance_to_target_km=0,
                        ),
                        provider_quality=ProviderQuality(),
                        raw_unit="mm",
                        provenance=Provenance(
                            operation_id=plan.operation_id,
                            page_identity=f"page:{plan.operation_id}",
                            provider_record_id=f"weather:{plan.operation_id}",
                        ),
                        grid_id="LWX/96,70",
                        weather_variable="precipitationLastHour",
                        value="2.5",
                        duration_seconds=3600,
                        statistic="interval_total",
                    ),
                ),
            )

        weather = FakeCollectionPort(weather_batch)
        scope, store, loop, validated = await _fixture(
            model,
            water_mode="partial",
            context=context,
            weather_port=weather,
        )
        scope_holder["scope"] = scope
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert weather.calls
        assert outcome.counts.rule_proposals == 1
        assert outcome.counts.agent_tool_calls == 1
        assert "collect_weather_evidence" not in {
            tool.name for tool in model.calls[0].tool_descriptors
        }
        assert "collect_weather_evidence" in {
            tool.name for tool in model.calls[1].tool_descriptors
        }
        assert len(outcome.counts.bundle_refs) == 4
        assert any(
            fragment.relation_id
            == outcome.handoff.rule_plan.appended_rules[0].rule_id
            for fragment in store.fragments.values()
        )
        assert outcome.counts.final_reasoning_eligible_cells >= 1

    asyncio.run(exercise())


def test_visible_only_and_over_limit_tool_responses_fail_before_optional_writes():
    async def exercise():
        visible_model = DeterministicWaterbodyModelPort(
            (
                VisibleTextDelta(
                    **_event_values(OPS[0]),
                    message_id="visible-only",
                    text="The evidence seems sufficient.",
                ),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-visible",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )
        )
        _, visible_store, visible_loop, visible_validated = await _fixture(
            visible_model
        )
        with pytest.raises(PlanningLoopError) as raised:
            await visible_loop.run(
                visible_validated,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )
        assert raised.value.code == "invalid_planning_response"
        assert {call[1].tool_name for call in visible_store.calls} == {
            "collect_water_observations", "aggregate_evidence",
        }
        assert not any(
            call[1].tool_name == "collect_weather_evidence"
            for call in visible_store.calls
        )

        context = make_context()
        calls = tuple(
            ToolCallCompleted(
                **_event_values(OPS[0]),
                provider_call_id=f"call-{index}",
                name="collect_weather_evidence",
                arguments=json.dumps(
                    {
                        "window_ids": ["window-000"],
                        "parameter_keys": [
                            context.params_used[0].key.model_dump(mode="json")
                        ],
                        "options": {},
                    },
                    separators=(",", ":"),
                ),
            )
            for index in range(5)
        )
        over_model = DeterministicWaterbodyModelPort(
            (
                *calls,
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-over",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )
        )
        _, over_store, over_loop, over_validated = await _fixture(over_model)
        with pytest.raises(PlanningLoopError) as raised:
            await over_loop.run(
                over_validated,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )
        assert raised.value.code == "planning_tool_call_limit_exceeded"
        assert {call[1].tool_name for call in over_store.calls} == {
            "collect_water_observations", "aggregate_evidence",
        }
        assert not any(
            call[1].tool_name == "collect_weather_evidence"
            for call in over_store.calls
        )

    asyncio.run(exercise())


def test_scope_violation_max_turn_deadline_and_cancel_boundaries():
    async def exercise():
        context = make_weather_context()
        invalid_model = DeterministicWaterbodyModelPort(
            _tool_script(
                OPS[0],
                call_id="outside-window",
                window_ids=("window-outside",),
                parameter_keys=(WEATHER_PARAMETER,),
            ),
            _ready_script(OPS[1]),
        )
        _, invalid_store, invalid_loop, invalid_validated = await _fixture(
            invalid_model,
            context=context,
            weather_port=FakeCollectionPort(lambda plan: complete_batch(plan)),
        )
        invalid = await invalid_loop.run(
            invalid_validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert invalid.counts.convergence_reason is (
            PlanningConvergenceReason.EVIDENCE_READY
        )
        assert invalid.counts.planning_model_calls == 2
        assert invalid.counts.agent_tool_calls == 1
        assert invalid.handoff.budget_usage.collection_used == 2
        assert [
            call[1].tool_name for call in invalid_store.calls
        ].count("collect_weather_evidence") == 1
        assert (
            invalid_store.results[-1][1].safe_error.code
            == "evidence_scope_violation"
        )
        assert "evidence_scope_violation" in "\n".join(
            message.text for message in invalid_model.calls[1].messages
        )

        max_model = DeterministicWaterbodyModelPort(
            _tool_script(
                OPS[0],
                call_id="weather-max",
                window_ids=("window-000",),
                parameter_keys=(WEATHER_PARAMETER,),
            )
        )
        _, _, max_loop, max_validated = await _fixture(
            max_model,
            context=context,
            weather_port=FakeCollectionPort(lambda plan: complete_batch(plan)),
            limits=PlanningLoopLimits(max_turns=1),
        )
        maximum = await max_loop.run(
            max_validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )
        assert maximum.counts.convergence_reason is PlanningConvergenceReason.MAX_TURNS

        deadline_model = DeterministicWaterbodyModelPort()
        _, _, deadline_loop, deadline_validated = await _fixture(deadline_model)
        deadline = await deadline_loop.run(
            deadline_validated,
            deadline=FixedClock().now(),
        )
        assert deadline.counts.convergence_reason is (
            PlanningConvergenceReason.DEADLINE_REACHED
        )
        assert deadline.counts.planning_model_calls == 0

        cancel_event = asyncio.Event()
        cancel_model = DeterministicWaterbodyModelPort(
            (
                ScriptedCancellation(cancel_event),
                Terminal(
                    **_event_values(OPS[0]),
                    provider_response_id="response-cancelled",
                    finish_reason=WaterbodyFinishReason.COMPLETED,
                ),
            )
        )
        _, _, cancel_loop, cancel_validated = await _fixture(
            cancel_model,
            cancel_event=cancel_event,
        )
        with pytest.raises(WaterbodyModelCancelledError):
            await cancel_loop.run(
                cancel_validated,
                deadline=FixedClock().now() + timedelta(minutes=5),
            )

    asyncio.run(exercise())
