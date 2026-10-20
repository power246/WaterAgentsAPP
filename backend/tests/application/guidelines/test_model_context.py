import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.canonical_json import canonical_json_digest, canonical_json_text
from app.application.guidelines.model_context import GuidelineModelContext
from app.application.reasoning import ModelFitCapability, PlanningLoopError, ValidationExchange
from app.application.reasoning.model_models import WaterbodyMessageRole, WaterbodyModelPhase
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.domain.guidelines import GuidelineMatch, GuidelinePolicy, GuidelineRetrievalResult, GuidelineUsageTarget
from app.infrastructure.runtime import OpenAIResponsesSizeEstimator
from app.ports import WaterbodyModelCancelledError, WaterbodyModelProtocolError, WaterbodyModelTimeoutError
from tests.application.evidence_tools.fakes import FixedClock
from tests.application.guidelines.test_retrieval import Repository, pin, task as retrieval_task
from tests.application.guidelines.test_worker import Embedding
from tests.application.reasoning.m7_support import batch_payload, m7_fixture, synthesis_payload
from tests.application.reasoning.test_numeric_estimation import EstimatePort, _estimator, _request
from tests.application.reasoning.test_planning_loop import AbstainingNumericModel, OPS, _fixture, _ready_script
from tests.application.reasoning.test_validation_repair import _completed_script
from tests.guideline_support import content, version


class Session:
    def __init__(self, versions, *, policy=None):
        self.pin = SimpleNamespace(run_id=uuid4())
        self.session_id = uuid4()
        self.policy = policy or GuidelinePolicy()
        self.loads = 0
        self.result = GuidelineRetrievalResult(status="retrieved" if versions else "no_match", matches=tuple(
            GuidelineMatch(version=item, similarity=1-index/10) for index, item in enumerate(versions)))

    async def get_or_load(self):
        self.loads += 1
        return self.result


def token():
    return CancellationToken(asyncio.Event())


def model_context(session, model, *, fit=None, clock=None):
    return GuidelineModelContext(session, model_fit=fit or ModelFitCapability(
        model=model, context_window_tokens=1_000_000, max_output_tokens=100_000),
        size_estimator=OpenAIResponsesSizeEstimator(), clock=clock or FixedClock())


def advisory(request):
    return [item for message in request.messages for item in json.loads(message.text).get("advisory_knowledge", [])]


async def real_context(validated=None, *, clock=None, embedding=None, repository=None):
    embedding, repository = embedding or Embedding(), repository or Repository()
    clock = clock or FixedClock()
    run = await pin(repository, embedding, clock=clock, run_id=validated.task.run_id if validated is not None else None)
    validated = validated or retrieval_task(run.pin.run_id, token())
    session = run.session(validated, deadline=clock.now()+timedelta(minutes=5))
    return session, embedding, repository


@pytest.mark.parametrize("skip_planning", [False, True])
def test_first_actual_planning_or_numeric_dispatch_loads_once(skip_planning):
    async def exercise():
        model = AbstainingNumericModel(_ready_script(OPS[0]))
        scope, _, loop, validated = await _fixture(model, water_mode="empty")
        repository = Repository()
        keys = tuple(dict.fromkeys((*scope.requested_parameters, *scope.supporting_parameters, *scope.intermediate_parameters)))
        repository.matches = (GuidelineMatch(version=version(content(parameter_keys=keys)), similarity=1),)
        session, embedding, _ = await real_context(validated, repository=repository)
        context = model_context(session, validated.task.waterbody_model)
        loop._guidelines = context
        original_stream = model.stream

        async def checked_stream(request):
            assert len(embedding.calls) == 1
            assert advisory(request)
            async for event in original_stream(request):
                yield event

        model.stream = checked_stream
        try:
            initial = await loop._acquisition.start(scope) if skip_planning else None
            if initial is not None:
                initial = replace(initial, stop_before_model=True)
            outcome = await loop.run(validated, deadline=FixedClock().now()+timedelta(minutes=5), initial_handoff=initial)
            assert outcome.counts.planning_model_calls == (0 if skip_planning else 1)
            assert outcome.counts.numeric_estimation_calls > 0
            assert model.calls[0].phase is (WaterbodyModelPhase.NUMERIC_ESTIMATION if skip_planning
                                           else WaterbodyModelPhase.EVIDENCE_PLANNING)
            assert len(embedding.calls) == len(repository.ranks) == 1
        finally:
            await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["zero_model", "cancelled", "deadline", "observed"])
def test_knowledge_never_creates_a_dispatch(boundary):
    async def exercise():
        stop = asyncio.Event()
        model = AbstainingNumericModel(_ready_script(OPS[0]))
        scope, _, loop, validated = await _fixture(model, cancel_event=stop)
        session, embedding, repository = await real_context(validated)
        context = model_context(session, validated.task.waterbody_model)
        loop._guidelines = context
        handoff = await loop._acquisition.start(scope)
        if boundary in {"zero_model", "observed"}:
            handoff = replace(handoff, stop_before_model=True)
        if boundary == "zero_model":
            handoff = replace(handoff, numeric_estimation_allowed=False)
        if boundary == "cancelled":
            stop.set()
        try:
            try:
                await loop.run(validated, initial_handoff=handoff, deadline=FixedClock().now()+timedelta(
                    seconds=0 if boundary == "deadline" else 300))
            except PlanningLoopError as error:
                assert boundary == "cancelled"
                assert error.code == "planning_cancelled"
            assert not model.calls and not embedding.calls and not repository.ranks
            assert not context.operations and not context.usages
        finally:
            await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("refs", ["valid", "missing", "empty", "unknown", "cross_operation"])
def test_numeric_aliases_are_optional_scoped_and_do_not_change_values(refs, caplog):
    async def exercise():
        session, embedding, _ = await real_context()
        port = EstimatePort()
        context = model_context(session, _estimator(port)._model)
        original = port.stream

        async def respond(request):
            assert len(embedding.calls) == 1 and advisory(request)
            alias = advisory(request)[0]["alias"]
            if refs != "missing":
                port.payload["guideline_refs"] = ([alias] if refs == "valid" else [] if refs == "empty" else
                    [f"G1_{uuid4().hex}"] if refs == "cross_operation" else ["https://invented.invalid/version", {"version_id": "fake"}])
            async for event in original(request):
                yield event

        port.stream = respond
        try:
            request = _request()
            result = await _estimator(port, guidelines=context)(request)
            assert (result.value, result.value_lower, result.value_upper) == (None, "6.2", "8.8")
            assert len(port.calls) == 1
            assert len(context.usages) == (1 if refs == "valid" else 0)
            if context.usages:
                usage = context.usages[0]
                assert str(usage.operation_id) == result.operation_id
                assert usage.target == GuidelineUsageTarget(kind="numeric_task", task_id=request.task.task_id,
                                                            window_id=request.task.window_id)
            assert "guideline_refs" not in result.__dataclass_fields__
            if refs in {"unknown", "cross_operation"}:
                assert any(record.event_name == "guideline_reference_discarded" for record in caplog.records)
                assert "invented.invalid" not in caplog.text and "fake" not in caplog.text
        finally:
            await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("repair", [False, True])
def test_batch_repair_and_synthesis_keep_only_adopted_snapshots(repair):
    async def exercise():
        skeleton, plan, catalog, validator = m7_fixture()
        batch = plan.batch_plan.batches[0]
        versions = tuple(version(content(parameter_keys=(batch.cells[0].parameter,), body_markdown=body)) for body in (
            "Consider mechanism A if condition A holds. Required local input is unavailable; retain uncertainty.",
            "Consider alternative B; its assumptions can conflict with A. The model may ignore either source."))
        session = Session(versions)
        context = model_context(session, batch.model)
        calls = []

        class Model:
            async def stream(self, request):
                calls.append(request)
                block = advisory(request)
                if request.phase is WaterbodyModelPhase.OVERALL_SYNTHESIS:
                    assert len(block) == 1 and block[0]["title"] == versions[0].content.title
                    payload = synthesis_payload(skeleton, plan, reasoning)
                    payload["overall"]["guideline_refs"] = [block[0]["alias"]]
                    payload["window_assessments"][0]["guideline_refs"] = [block[0]["alias"]]
                    payload["analysis_detail"]["steps"][0]["guideline_refs"] = [block[0]["alias"]]
                else:
                    assert len(block) == 2
                    payload = batch_payload(batch, catalog)
                    payload["cells"][0]["guideline_refs"] = [block[0]["alias"], "foreign-operation"]
                    if repair and request.phase is WaterbodyModelPhase.REASONING_BATCH:
                        payload["scope_digest"] = "sha256:wrong"
                for event in _completed_script(request.operation_id, request.model, payload):
                    yield event

        exchange = ValidationExchange(model_port=Model(), prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
                                      validator=validator, guidelines=context)
        outcome = await exchange.run_batch(batch, cancellation=token())
        accepted = outcome.validated
        baseline = validator.validate_batch(batch, batch_payload(batch, catalog))
        assert accepted.validation_digest == baseline.validation_digest
        assert accepted.model_dump(mode="json") == baseline.model_dump(mode="json")
        if repair:
            assert advisory(calls[0]) == advisory(calls[1])
            assert calls[0].scope_digest == calls[1].scope_digest
            assert outcome.audit.repair_request == calls[1]
        assert outcome.audit.initial_request == calls[0]
        reasoning = validator.complete_batches(plan.batch_plan, (accepted,))
        await exchange.run_synthesis(skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, cancellation=token())
        assert session.loads == 1
        assert [usage.target.kind for usage in context.usages] == ["cell", "overall", "window", "analysis_step"]
        assert all(usage.versions == (versions[0],) for usage in context.usages)
        assert len(calls) == (3 if repair else 2)
    asyncio.run(exercise())


def test_synthesis_does_not_search_without_adopted_references():
    async def exercise():
        skeleton, plan, catalog, validator = m7_fixture()
        from tests.application.reasoning.m7_support import validated_reasoning
        reasoning = validated_reasoning(plan, validator, catalog)
        session = Session((version(),))
        context = model_context(session, plan.batch_plan.batches[0].model)

        class Model:
            async def stream(self, request):
                assert not advisory(request)
                for event in _completed_script(request.operation_id, request.model, synthesis_payload(skeleton, plan, reasoning)):
                    yield event

        await ValidationExchange(model_port=Model(), prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
                                 validator=validator, guidelines=context).run_synthesis(
            skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, cancellation=token())
        assert session.loads == 0 and not context.usages
    asyncio.run(exercise())


@pytest.mark.parametrize("keep", [0, 1, 2])
def test_final_serialized_request_preserves_evidence_and_output_reserve(keep):
    async def exercise():
        _, plan, catalog, validator = m7_fixture()
        batch = plan.batch_plan.batches[0]
        request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(batch.prompt_input())
        versions = tuple(version(content(parameter_keys=(batch.cells[0].parameter,), body_markdown=f"Whole mechanism {i}. "*30))
                         for i in range(2))
        session = Session(versions)
        wide = model_context(session, request.model)
        full = await wide.prepare(request, cancellation=token(), targets=tuple(cell.cell_id for cell in batch.cells))
        assert len(advisory(full)) == 2
        if keep == 1:
            block = advisory(full)[:1]
            bounded = full.model_copy(update={"messages": (*request.messages, full.messages[-1].model_copy(
                update={"text": canonical_json_text({"advisory_knowledge": block})}))})
        else:
            bounded = full if keep else request
        estimator = OpenAIResponsesSizeEstimator()
        shape = canonical_json_text({"response_schema": json.loads(request.response_schema.schema_text),
                                     "cell_ids": tuple(cell.cell_id for cell in batch.cells)}).encode()
        size = estimator.estimate(request.model, serialized_request=canonical_json_text(bounded.model_dump(mode="json")).encode(),
                                  serialized_response_shape=shape)
        fit = ModelFitCapability(model=request.model, context_window_tokens=size.input_tokens+size.output_tokens,
                                 max_output_tokens=size.output_tokens)
        context = model_context(session, request.model, fit=fit)
        calls = []

        class Model:
            async def stream(self, actual):
                calls.append(actual)
                for event in _completed_script(actual.operation_id, actual.model, batch_payload(batch, catalog)):
                    yield event

        outcome = await ValidationExchange(model_port=Model(), prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
                                           validator=validator, guidelines=context).run_batch(batch, cancellation=token())
        actual, = calls
        assert outcome.audit.initial_request == actual
        assert len(advisory(actual)) == keep
        assert actual.messages[:len(request.messages)] == request.messages
        assert actual.system_prompt == request.system_prompt and actual.tool_descriptors == request.tool_descriptors
        assert all(item.content.body_markdown not in actual.system_prompt for item in versions)
        assert context.aliases(request.operation_id) == frozenset(item["alias"] for item in advisory(actual))
        selection = context.operations[request.operation_id]
        assert selection.request_digest == canonical_json_digest(actual.model_dump(mode="json"))
        assert selection.status == ("budget_limited" if not keep else "retrieved")
        if keep:
            assert actual.messages[-1].role is WaterbodyMessageRole.USER
            assert advisory(actual)[-1]["body"] == versions[keep-1].model_view.body
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["cancelled", "deadline"])
def test_retrieval_finishing_after_boundary_never_dispatches_reasoning(boundary):
    async def exercise():
        clock, stop = FixedClock(), asyncio.Event()
        request = _request(cancel_event=stop)

        async def cross_boundary(*_args):
            if boundary == "cancelled":
                stop.set()
            else:
                clock.value += timedelta(minutes=6)

        session, embedding, _ = await real_context(clock=clock, embedding=Embedding(cross_boundary))
        port = EstimatePort()
        context = model_context(session, _estimator(port)._model, clock=clock)
        try:
            with pytest.raises(WaterbodyModelCancelledError if boundary == "cancelled" else WaterbodyModelTimeoutError):
                await _estimator(port, guidelines=context)(request)
            assert len(embedding.calls) == 1 and not port.calls and not context.usages
        finally:
            await session.aclose()
    asyncio.run(exercise())


def test_numeric_usage_links_saved_scientific_payload_and_selected_dependency_only():
    async def exercise():
        port = EstimatePort()

        class Model(AbstainingNumericModel):
            async def stream(self, request):
                if request.phase is not WaterbodyModelPhase.NUMERIC_ESTIMATION:
                    async for event in super().stream(request):
                        yield event
                    return
                port.payload["guideline_refs"] = [advisory(request)[0]["alias"]]
                async for event in port.stream(request):
                    yield event

        model = Model(_ready_script(OPS[0]))
        scope, _, loop, validated = await _fixture(model, water_mode="empty")
        keys = tuple(dict.fromkeys((*scope.requested_parameters, *scope.supporting_parameters, *scope.intermediate_parameters)))
        selected = version(content(parameter_keys=keys))
        unselected = version(content(parameter_keys=keys))
        session = Session((selected, unselected))
        context = model_context(session, validated.task.waterbody_model)
        loop._guidelines = context
        outcome = await loop.run(validated, deadline=FixedClock().now()+timedelta(minutes=5))
        fragments = tuple([await outcome.handoff.read_port.read_fragment(scope, ref)
                           for ref in outcome.handoff.fragment_allowlist])
        numeric = tuple(fragment for fragment in fragments if fragment.payload.payload_type == "numeric_derived")
        assert numeric and len(context.usages) == len(port.calls)
        for fragment in numeric:
            usage = next(usage for usage in context.usages if str(usage.operation_id) == fragment.payload.model_operation_id)
            assert fragment.payload.task_id == usage.target.task_id
            assert usage.target.window_id in {window.window_id for window in scope.time_windows
                                              if window.time_range == fragment.time_range}
            assert "guideline_refs" not in fragment.payload.model_dump()
        # A second candidate operation adopts a different document but has no selected result lineage.
        previous = context.usages[0]
        context.usages.append(replace(previous, operation_id=uuid4(), versions=(unselected,)))
        context.bind_evidence(fragments)
        reasoning = SimpleNamespace(batches=(SimpleNamespace(cells=(SimpleNamespace(
            cell_id="selected-cell", evidence_refs=(numeric[0].evidence_id,)),)),))
        assert context.synthesis_versions(reasoning) == {selected.version_id}
        # Converted evidence retains the numeric origin without adopting unrelated candidates.
        converted = SimpleNamespace(evidence_id="converted", payload=SimpleNamespace(
            input_evidence_ref=SimpleNamespace(evidence_id=numeric[0].evidence_id)))
        context.bind_evidence((*fragments, converted))
        reasoning.batches[0].cells[0].evidence_refs = ("converted",)
        assert context.synthesis_versions(reasoning) == {selected.version_id}
    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["empty_library", "no_match", "unavailable", "budget_limited"])
def test_unavailable_or_omitted_guidelines_do_not_gate_a_valid_numeric_range(status):
    async def exercise():
        session = Session(())
        if status == "budget_limited":
            session = Session((version(),), policy=GuidelinePolicy(knowledge_budget_tokens=1))
        else:
            session.result = GuidelineRetrievalResult(status=status)
        port = EstimatePort()
        context = model_context(session, _estimator(port)._model)
        result = await _estimator(port, guidelines=context)(_request())
        assert (result.value_lower, result.value_upper) == ("6.2", "8.8")
        assert len(port.calls) == session.loads == 1 and not advisory(port.calls[0])
        assert not context.usages
        assert context.operations[port.calls[0].operation_id].status == status
    asyncio.run(exercise())


def test_valid_guideline_alias_never_authorizes_an_unanchored_point():
    async def exercise():
        port = EstimatePort(value="7.0")
        session = Session((version(),))
        context = model_context(session, _estimator(port)._model)
        original = port.stream

        async def respond(request):
            port.payload["guideline_refs"] = [advisory(request)[0]["alias"]]
            async for event in original(request):
                yield event

        port.stream = respond
        with pytest.raises(ValueError, match="point estimate requires original same-parameter measurements"):
            await _estimator(port, guidelines=context)(_request())
        assert len(port.calls) == 1 and not context.usages
    asyncio.run(exercise())


def test_oversize_base_request_cannot_spend_output_reserve_or_dispatch():
    async def exercise():
        port = EstimatePort()
        context = model_context(Session(()), _estimator(port)._model, fit=ModelFitCapability(
            model=_estimator(port)._model, context_window_tokens=10, max_output_tokens=5))
        with pytest.raises(WaterbodyModelProtocolError):
            await _estimator(port, guidelines=context)(_request())
        assert not port.calls and not context.operations
    asyncio.run(exercise())


def test_composed_runtime_records_the_same_advisory_requests_and_completes(monkeypatch):
    from app.application.reasoning import WaterbodyAgentRunStatus
    from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort
    from tests.m6_reasoning_support import create_reasoning_test_app, prepare_waterbody

    async def exercise():
        embedding = Embedding()

        class Model(AdaptiveWaterbodyModelPort):
            @staticmethod
            def _body(request):
                return next(json.loads(message.text) for message in reversed(request.messages)
                            if "advisory_knowledge" not in json.loads(message.text))

            def _payload(self, request):
                assert len(embedding.calls) == 1 and advisory(request)
                payload = super()._payload(request)
                if request.phase is WaterbodyModelPhase.REASONING_BATCH:
                    payload["cells"][0]["guideline_refs"] = [advisory(request)[0]["alias"]]
                if request.phase is WaterbodyModelPhase.OVERALL_SYNTHESIS:
                    payload["overall"]["guideline_refs"] = [advisory(request)[0]["alias"]]
                return payload

        model = Model()
        application, workflow, water = create_reasoning_test_app(monkeypatch, model_port=model)
        async with application.router.lifespan_context(application):
            prepared = await prepare_waterbody(application, workflow, water)
            task = prepared.task
            scope = task.task.evidence_scope
            repository = Repository()
            repository.matches = (GuidelineMatch(version=version(content(parameter_keys=scope.requested_parameters,
                related_parameter_keys=scope.supporting_parameters)), similarity=1),)
            runtime = application.state.m6_runtime.runner
            session, _, _ = await real_context(task, clock=runtime._clock, embedding=embedding, repository=repository)
            try:
                task = replace(task, task=replace(task.task, guidelines=session))
                runner = runtime.build_runner(task)
                outcome = await runner.run(task, deadline=runtime.session_deadline()-timedelta(seconds=1))
                assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
                assert [request.phase for request in model.calls] == [WaterbodyModelPhase.EVIDENCE_PLANNING,
                    WaterbodyModelPhase.REASONING_BATCH, WaterbodyModelPhase.OVERALL_SYNTHESIS]
                assert [usage.target.kind for usage in runner._guidelines.usages] == ["cell", "overall"]
                assert len(repository.ranks) == len(embedding.calls) == 1
                assert "guideline_refs" not in canonical_json_text(outcome.result.model_dump(mode="json"))
            finally:
                await session.aclose()
    asyncio.run(exercise())
