import asyncio
import json
from datetime import timedelta
from uuid import UUID

import pytest

from app.application.reasoning import (
    EvidencePlanningLoop,
    PlanningConvergenceReason,
    PlanningLoopError,
    PlanningLoopLimits,
    ReasoningGateDecision,
    validate_waterbody_agent_task,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyFinishReason,
)
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.domain import (
    EvidenceLocation,
    ProviderQuality,
    Provenance,
    WeatherObservationRecord,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import WaterbodyModelCancelledError
from tests.application.evidence_tools.conftest import (
    MODEL,
    make_context,
    make_graph,
    make_scope,
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
    upstream_port=None,
    upstream_ready=False,
    cancel_event=None,
    limits=PlanningLoopLimits(),
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
    )
    return scope, store, loop, validated


def test_station_backed_empty_is_zero_planning_and_zero_final_reasoning():
    async def exercise():
        model = DeterministicWaterbodyModelPort()
        _, store, loop, validated = await _fixture(model, water_mode="empty")
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert model.calls == []
        assert outcome.counts.planning_model_calls == 0
        assert outcome.counts.final_reasoning_eligible_cells == 0
        assert outcome.counts.reasoning_batch_calls == 0
        assert outcome.counts.synthesis_calls == 0
        assert outcome.counts.convergence_reason is (
            PlanningConvergenceReason.STATION_BACKED_EMPTY
        )
        assert len(store.bundles) == 1

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


def test_multi_turn_tools_rebuild_bundles_and_exact_duplicates_reuse_provider_work():
    async def exercise():
        weather = FakeCollectionPort(lambda plan: complete_batch(plan))
        model = DeterministicWaterbodyModelPort(
            _tool_script(
                OPS[0],
                call_id="weather-a",
                window_ids=("window-000",),
                parameter_keys=(make_context().params_used[0].key,),
            ),
            _tool_script(
                OPS[1],
                call_id="weather-b",
                window_ids=("window-000",),
                parameter_keys=(make_context().params_used[0].key,),
            ),
            _ready_script(OPS[2]),
        )
        _, store, loop, validated = await _fixture(
            model,
            weather_port=weather,
        )
        outcome = await loop.run(
            validated,
            deadline=FixedClock().now() + timedelta(minutes=5),
        )

        assert len(weather.calls) == 1
        assert outcome.counts.agent_tool_calls == 2
        assert outcome.counts.planning_model_calls == 3
        assert len(outcome.counts.bundle_refs) == 3
        assert len({ref.item_id for ref in outcome.counts.bundle_refs}) == 3
        assert set(store.bundles[0][1].fragment_refs) <= set(
            store.bundles[-1][1].fragment_refs
        )
        assert store.bundles[0][1] != store.bundles[-1][1]

    asyncio.run(exercise())


def test_appended_weather_rule_without_weather_cannot_unlock_gate():
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
        model = DeterministicWaterbodyModelPort(
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
            gate.decision is ReasoningGateDecision.UNSUPPORTED
            for gate in outcome.sealed_input.gates
        )
        assert outcome.counts.final_reasoning_eligible_cells == 0
        assert len(store.bundles) == 2

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
        model = DeterministicWaterbodyModelPort(
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
        assert len(outcome.counts.bundle_refs) == 3
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
        assert len(visible_store.calls) == 1

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
        assert len(over_store.calls) == 1

    asyncio.run(exercise())


def test_scope_violation_max_turn_deadline_and_cancel_boundaries():
    async def exercise():
        context = make_context()
        invalid_model = DeterministicWaterbodyModelPort(
            _tool_script(
                OPS[0],
                call_id="outside-window",
                window_ids=("window-outside",),
                parameter_keys=(context.params_used[0].key,),
            ),
            _ready_script(OPS[1]),
        )
        _, invalid_store, invalid_loop, invalid_validated = await _fixture(
            invalid_model,
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
        assert len(invalid_store.calls) == 2
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
                parameter_keys=(context.params_used[0].key,),
            )
        )
        _, _, max_loop, max_validated = await _fixture(
            max_model,
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
