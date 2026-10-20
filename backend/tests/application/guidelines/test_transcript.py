import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from app.application.analysis_runs.models import RunStatus, TranscriptItemIdentity
from app.application.analysis_runs.workflow import InvalidRunTransitionError
from app.application.canonical_json import canonical_json_text
from app.application.guidelines.transcript import GuidelineTranscriptWriteError
from app.application.guidelines.transcript_models import GUIDELINE_RECORDS
from app.application.guidelines.transcript_validation import validate_guideline_links
from app.application.reasoning import WaterbodyAgentRunStatus
from app.application.reasoning.model_models import WaterbodyModelPhase
from app.application.reasoning.model_protocol import consume_waterbody_model_stream
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.application.transcript_memory import TranscriptRAGCaseSourceReader, build_prepared_projection
from app.application.transcripts.read_models import ReadView
from app.domain.guidelines import GuidelineMatch
from app.infrastructure.config import AppSettings
from app.ports import WaterbodyModelUnavailableError
from scripts.run_sse_smoke import running_server
from scripts.test_database import temporary_migrated_schema
from tests.application.guidelines.test_model_context import advisory, real_context
from tests.application.guidelines.test_retrieval import Repository
from tests.application.reasoning.m7_support import m7_fixture
from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort
from tests.guideline_support import content, source, version
from tests.m6_reasoning_support import cancel_run, create_reasoning_test_app, prepare_waterbody
from tests.persistence.test_m6_reasoning_persistence import _wait_persisted


class AdvisoryModel(AdaptiveWaterbodyModelPort):
    def __init__(self, *, cite=True, fail=False, numeric=False):
        super().__init__()
        self.cite, self.fail, self.numeric = cite, fail, numeric

    @staticmethod
    def _body(request):
        body = next(json.loads(message.text) for message in reversed(request.messages)
                    if "advisory_knowledge" not in json.loads(message.text))
        for field in ("evidence_summaries", "bounded_reference_summaries"):
            for entry in body.get(field, []):
                summary = json.loads(entry["summary"])
                if summary.get("method_kind") == "model_estimate":
                    system, code = summary["param"].split(":")
                    entry["summary"] = json.dumps({"payload": {"payload_type": "numeric_derived", **summary},
                        "parameters": [{"code_system": system, "code": code}], "quality_class": "suspicious",
                        "quality_flags": summary["flags"].split(",") if summary["flags"] else [],
                        "applicability": "allowed_indirect", "result_eligibility": "inferred_only"})
        return body

    def _payload(self, request):
        if self.fail:
            raise WaterbodyModelUnavailableError(provider=request.model.provider, model=request.model.model,
                                                 operation_id=str(request.operation_id))
        payload = super()._payload(request)
        if self.numeric and request.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION:
            payload.update(value_lower="6.2", value_upper="8.8", confidence=0.2,
                rationale="Synthetic low-basis range; local measurements are unavailable.")
        if self.numeric and request.phase is WaterbodyModelPhase.REASONING_BATCH:
            body = self._body(request)
            summaries = {entry["evidence_ref"]: entry for entry in body["evidence_summaries"]}
            details = {key: json.loads(entry["summary"]) for key, entry in summaries.items()}
            for planned, cell in zip(body["cells"], payload["cells"]):
                refs = [ref for ref in planned["allowed_evidence_refs"]
                        if details[ref]["payload"]["payload_type"] == "numeric_derived"
                        and planned["parameter"] in details[ref]["parameters"]]
                target = refs[0]
                support = next((ref for ref in planned["allowed_evidence_refs"]
                    if details[ref]["payload"]["payload_type"] == "numeric_derived"
                    and {"code_system": "usgs", "code": "00060"} in details[ref]["parameters"]), None)
                refs = sorted({target, support} - {None})
                values = details[target]["payload"]
                cell.update(outcome_status="range_estimate", representative_value=None,
                    value_lower=values["value_lower"], value_upper=values["value_upper"],
                    target_unit_id=values["unit"], confidence=0.2, aggregation_method=None,
                    evidence_refs=refs, series_refs=sorted({series for ref in refs for series in summaries[ref]["series_refs"]}),
                    quality_flags=sorted({flag for ref in refs for flag in details[ref]["quality_flags"]}))
        block = advisory(request)
        if self.cite and block:
            refs = [block[0]["alias"]] if self.cite is True else ["foreign-alias"]
            if request.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION:
                payload["guideline_refs"] = refs
            elif request.phase is WaterbodyModelPhase.REASONING_BATCH:
                payload["cells"][0]["guideline_refs"] = refs
            elif request.phase is WaterbodyModelPhase.OVERALL_SYNTHESIS:
                payload["overall"]["guideline_refs"] = refs
                payload["window_assessments"][0]["guideline_refs"] = refs
                payload["analysis_detail"]["steps"][0]["guideline_refs"] = refs
        return payload


@asynccontextmanager
async def scenario(monkeypatch, *, model=None, settings=None, postgres=False, wire=False, water_mode="full"):
    model = model or AdvisoryModel()
    settings = settings or AppSettings(_env_file=None, log_level="warning", memory_enabled=False)
    options = {"transcript_store": None} if postgres else {}
    application, workflow, water = create_reasoning_test_app(monkeypatch, model_port=model, settings=settings,
                                                          water_mode=water_mode, **options)
    resource = running_server(application) if wire else application.router.lifespan_context(application)
    async with resource as client:
        prepared = await prepare_waterbody(application, workflow, water)
        runtime = application.state.m6_runtime.runner
        repository = Repository()
        scope = prepared.task.task.evidence_scope
        keys = tuple(dict.fromkeys((*scope.requested_parameters, *scope.supporting_parameters, *scope.intermediate_parameters)))
        original = version(content(title="M9 saved original", parameter_keys=keys, sources=(source(),),
            body_markdown="Synthetic mechanism A has conditions and alternatives. Keep the original source snapshot."))
        repository.matches = (GuidelineMatch(version=original, similarity=1),)
        session, embedding, _ = await real_context(prepared.task, clock=runtime._clock, repository=repository)
        task = replace(prepared.task, task=replace(prepared.task.task, guidelines=session))
        runner = runtime.build_runner(task)
        state = SimpleNamespace(app=application, task=task, prepared=prepared, runner=runner, model=model, client=client,
            context=runner._guidelines, session=session, embedding=embedding, repository=repository, version=original,
            deadline=runtime.session_deadline()-timedelta(seconds=1))
        try:
            yield state
        finally:
            await session.aclose()


async def records(state, view=ReadView.AUTO):
    snapshot = await state.app.state.transcript_reader.read_registered_run(state.task.task.run_id, view)
    return snapshot, {entry.item.item_id: entry for entry in snapshot.items}


async def run_state(state):
    outcome = await state.runner.run(state.task, deadline=state.deadline)
    await _wait_persisted(state.app, state.task.task.run_id)
    return outcome


def test_constructing_request_records_retrieval_but_never_delivery(monkeypatch):
    async def exercise():
        async with scenario(monkeypatch) as state:
            _, plan, _, _ = m7_fixture()
            request = PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY).build_reasoning_batch(plan.batch_plan.batches[0].prompt_input())
            request = request.model_copy(update={"model": state.task.task.waterbody_model, "deadline": state.deadline})
            await state.context.prepare(request, cancellation=state.task.task.cancellation_token)
            _, items = await records(state)
            kinds = [entry.item.content_type for entry in items.values()]
            assert kinds.count("guideline_retrieval") == kinds.count("guideline_run_context") == 1
            assert "guideline_delivery" not in kinds and not state.model.calls
            assert not state.context.transcript.deliveries
    asyncio.run(exercise())


@pytest.mark.parametrize("cite,fail", [(False, False), (True, False), ("invalid", False), (False, True)])
def test_submission_transport_and_adoption_are_distinct_and_scoped(monkeypatch, cite, fail):
    async def exercise():
        async with scenario(monkeypatch, model=AdvisoryModel(cite=cite, fail=fail)) as state:
            outcome = await run_state(state)
            snapshot, items = await records(state)
            deliveries = [entry for entry in items.values() if entry.item.content_type == "guideline_delivery"]
            assert len(deliveries) == len(state.model.calls) == (1 if fail else 3)
            for delivered in deliveries:
                call = next(call for call in state.model.calls if call.operation_id == delivered.body.operation_id)
                expected = [{"alias": entry.alias, **entry.version.model_view.model_dump(mode="json")}
                            for entry in delivered.body.entries]
                assert advisory(call) == expected
                assert not delivered.item.rag_eligible
                validate_guideline_links(delivered.body, delivered.item, items.get)
            usages = [entry for entry in items.values() if entry.item.content_type == "guideline_usage"]
            assert len(usages) == (4 if cite is True and not fail else 0)
            if fail:
                assert outcome.status is WaterbodyAgentRunStatus.FAILED
                response = next(entry.body for entry in items.values() if entry.item.content_type == "waterbody_model_response")
                assert response.finish_reason.value == "error" and not usages
            else:
                assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
                result = next(entry for entry in items.values() if entry.item.content_type == "inference_result")
                validate_guideline_links(result.body, result.item, items.get)
                sidecars = [entry for entry in items.values() if entry.item.content_type == "result_guideline_usage"]
                assert len(sidecars) == (1 if cite is True else 0)
                if sidecars:
                    assert str(sidecars[0].item.item_id) in outcome.result.analysis_detail.transcript_item_refs
                    assert {binding.target.kind for binding in sidecars[0].body.bindings} == {"cell", "overall", "window", "analysis_step"}
                    assert next(binding.target.step_index for binding in sidecars[0].body.bindings
                                if binding.target.kind == "analysis_step") == 0
            assert all(not entry.item.rag_eligible for entry in snapshot.items if entry.item.content_type in GUIDELINE_RECORDS)
            public = await state.app.state.transcript_reader.get_content(state.task.task.run_id,
                deliveries[0].item.item_id, ReadView.AUTO)
            assert json.loads(public.text)["entries"][0]["version"]["content"]["sources"][0]["url"] == state.version.content.sources[0].url
            assert "provider_response_id" not in public.text and "m6-fixture-secret" not in public.text
    asyncio.run(exercise())


@pytest.mark.parametrize("rejected", tuple(GUIDELINE_RECORDS))
def test_writer_rejection_never_becomes_an_unrecorded_knowledge_call_or_numeric_failure(monkeypatch, rejected):
    async def exercise():
        async with scenario(monkeypatch) as state:
            coordinator = state.app.state.run_coordinator
            original = coordinator.finalize_transcript_item

            async def reject(run_id, item_id, content_type, *args, **kwargs):
                if content_type == rejected:
                    raise ValueError("synthetic writer rejection")
                return await original(run_id, item_id, content_type, *args, **kwargs)

            monkeypatch.setattr(coordinator, "finalize_transcript_item", reject)
            with pytest.raises(GuidelineTranscriptWriteError) as error:
                await state.runner.run(state.task, deadline=state.deadline)
            assert error.value.failed_stage == "transcript_write"
            if rejected in {"guideline_run_context", "guideline_retrieval", "guideline_delivery"}:
                assert not state.model.calls and not state.context.transcript.deliveries
            runtime = coordinator._runs[state.task.task.run_id]
            assert not any(item.content.content_type == "inference_result" for item in runtime.transcript_buffer.items)
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["cancelled", "deadline", "terminal"])
def test_cancel_deadline_and_terminal_never_add_a_delivery(monkeypatch, boundary):
    async def exercise():
        async with scenario(monkeypatch) as state:
            if boundary == "terminal":
                await run_state(state)
            if boundary == "cancelled":
                await cancel_run(state.app, state.task.task.run_id)
            if boundary == "deadline":
                state.deadline = state.app.state.m6_runtime.runner._clock.now()
            before = len(state.model.calls), len(state.context.transcript.deliveries)
            if boundary == "terminal":
                call = state.model.calls[0]
                request = call.model_copy(update={"operation_id": uuid4(), "messages": tuple(
                    message for message in call.messages if "advisory_knowledge" not in json.loads(message.text))})
                request = await state.context.prepare(request, cancellation=state.task.task.cancellation_token)
                with pytest.raises(GuidelineTranscriptWriteError):
                    await consume_waterbody_model_stream(state.runner._model_port, request,
                                                         cancellation=state.task.task.cancellation_token)
            else:
                try:
                    await state.runner.run(state.task, deadline=state.deadline)
                except InvalidRunTransitionError:
                    assert boundary == "cancelled"
            assert (len(state.model.calls), len(state.context.transcript.deliveries)) == before
    asyncio.run(exercise())


def test_real_session_and_run_sse_expose_the_same_provided_snapshot(monkeypatch):
    async def exercise():
        settings = AppSettings(_env_file=None, log_level="warning", memory_enabled=False, transcript_inline_max_bytes=131072)
        async with scenario(monkeypatch, settings=settings, wire=True) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            run_id, session_id = state.task.task.run_id, state.task.task.session_id
            session_events = await state.client.get(f"/api/analysis/runs/{run_id}/sessions/{session_id}/events")
            assert session_events.status_code == 200 and "event: stream_end" in session_events.text
            frames = [json.loads(line[6:]) for line in session_events.text.splitlines() if line.startswith("data: ")]
            delivered = [frame["payload"]["item"] for frame in frames
                         if frame.get("payload", {}).get("item", {}).get("content_type") == "guideline_delivery"]
            assert len(delivered) == len(state.model.calls)
            for item in delivered:
                body = item["content"]["body"]
                call = next(call for call in state.model.calls if str(call.operation_id) == body["operation_id"])
                assert [entry["alias"] for entry in body["entries"]] == [entry["alias"] for entry in advisory(call)]
            await cancel_run(state.app, run_id)
            run_events = await state.client.get(f"/api/analysis/runs/{run_id}/events")
            assert run_events.status_code == 200 and "event: run_snapshot" in run_events.text
            run_detail = await state.client.get(f"/api/analysis/runs/{run_id}")
            assert run_detail.status_code == 200
            assert any(item["content_type"] == "guideline_run_context" for item in run_detail.json()["items"])
            assert "m6-fixture-secret" not in run_events.text
    asyncio.run(exercise())


def test_postgres_restart_history_keeps_original_versions_sources_and_closed_links(monkeypatch):
    async def exercise(base_settings):
        settings = base_settings.model_copy(update={"log_level": "warning", "memory_enabled": False,
            "db_operation_timeout_seconds": 30, "transcript_inline_max_bytes": 768, "transcript_preview_bytes": 80})
        async with scenario(monkeypatch, settings=settings, postgres=True) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            run_id, session_id = state.task.task.run_id, state.task.task.session_id
            snapshot, items = await records(state, ReadView.HISTORY)
            originals = {key: entry.body.model_dump(mode="json") for key, entry in items.items()
                         if entry.item.content_type in GUIDELINE_RECORDS}
            delivery_id = next(key for key in originals if items[key].item.content_type == "guideline_delivery")
            reads = state.repository.reads
            for revision, action in enumerate(("publish", "disable", "restore", "bundle_update"), start=900):
                replacement = version(content(title=action, parameter_keys=state.version.content.parameter_keys))
                state.repository.matches = () if action == "disable" else (GuidelineMatch(version=replacement, similarity=1),)
                state.repository.catalog = state.repository.catalog.model_copy(update={"revision": revision, "generation_id": uuid4()})
                saved = await state.app.state.transcript_reader.get_content(run_id, delivery_id, ReadView.HISTORY)
                assert json.loads(saved.text) == originals[delivery_id]
                assert state.repository.reads == reads
            await cancel_run(state.app, run_id)
            await _wait_persisted(state.app, run_id)
        app, _, _ = create_reasoning_test_app(monkeypatch, model_port=AdvisoryModel(), settings=settings, transcript_store=None)
        async with app.router.lifespan_context(app):
            reader = app.state.transcript_reader
            restored = await reader.read_registered_run(run_id, ReadView.HISTORY)
            by_id = {entry.item.item_id: entry for entry in restored.items}
            for item_id, original in originals.items():
                entry = by_id[item_id]
                assert entry.parse_error is None and entry.body.model_dump(mode="json") == original
                validate_guideline_links(entry.body, entry.item, by_id.get)
                fetched = await reader.get_content(run_id, item_id, ReadView.HISTORY)
                assert json.loads(fetched.text) == original
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(f"/api/analysis/runs/{run_id}/sessions/{session_id}/transcript?view=history")
                assert response.status_code == 200
                assert not response.json()["issues"]
                assert {item["item_id"] for item in response.json()["items"] if item["content_type"] in GUIDELINE_RECORDS}
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_selected_numeric_lineage_excludes_a_second_candidate_for_the_same_task_window(monkeypatch):
    from app.application.evidence_tools.numeric_estimation import NumericEstimateRequest
    from app.application.reasoning.numeric_estimation import ModelNumericEstimator
    from app.domain import NumericPlanTask
    from app.infrastructure.config import load_parameter_capability_registry

    async def exercise():
        async with scenario(monkeypatch, model=AdvisoryModel(numeric=True), water_mode="empty") as state:
            await state.runner._transcript_store.write_agent_input(state.task)
            planning = await state.runner._planning_loop.run(state.task, deadline=state.deadline)
            original = next(call for call in state.model.calls if call.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION)
            task = NumericPlanTask.model_validate(json.loads(original.messages[0].text)["task"])
            scope = state.task.task.evidence_scope
            fragments = tuple([await planning.handoff.read_port.read_fragment(scope, ref)
                               for ref in original.numeric_input_refs])
            estimator = ModelNumericEstimator(model_port=state.runner._model_port, model=original.model,
                bundle_ref=planning.handoff.bundle_ref, capability_registry=load_parameter_capability_registry(),
                deadline=state.deadline, clock=state.app.state.m6_runtime.runner._clock, guidelines=state.context)
            extra = await estimator(NumericEstimateRequest(scope=scope, task=task, plan_ref=original.numeric_plan_ref,
                input_refs=original.numeric_input_refs, input_fragments=fragments, context_refs=(), context_fragments=(), measured_anchors=()))
            assert extra.operation_id != str(original.operation_id)
            outcome = await state.runner.run(state.task, deadline=state.deadline, initial_handoff=planning.handoff)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            _, items = await records(state)
            extra_usage = next(entry for entry in items.values() if entry.item.content_type == "guideline_usage"
                               and str(entry.body.operation_id) == extra.operation_id)
            assert extra_usage.body.target.task_id == task.task_id and extra_usage.body.target.window_id == task.window_id
            final = next(entry for entry in items.values() if entry.item.content_type == "result_guideline_usage")
            included = {item_id for binding in final.body.bindings for item_id in binding.usage_item_ids}
            assert extra_usage.item.item_id not in included
            assert any(items[item_id].body.target.kind == "numeric_task" for item_id in included)
            result = next(entry for entry in items.values() if entry.item.content_type == "inference_result")
            validate_guideline_links(result.body, result.item, items.get)
    asyncio.run(exercise())


@pytest.mark.parametrize("corruption", ["run", "session", "version", "operation", "cycle", "window"])
def test_registry_writer_rejects_fabricated_citations_and_result_targets(monkeypatch, corruption):
    async def exercise():
        async with scenario(monkeypatch) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED
            runtime = state.app.state.run_coordinator._runs[state.task.task.run_id]
            buffer = runtime.transcript_buffer.snapshot_for_storage()
            _, items = await records(state)
            usage = next(entry for entry in items.values() if entry.item.content_type == "guideline_usage"
                         and entry.body.target.kind == "cell")
            body = usage.body.model_dump(mode="json")
            content_type = "guideline_usage"
            item_id = uuid4()
            if corruption in {"run", "session"}:
                body[corruption+"_id"] = str(uuid4())
            elif corruption == "version":
                body["citations"][0]["version_id"] = str(uuid4())
            elif corruption == "operation":
                body["operation_id"] = str(uuid4())
            elif corruption == "cycle":
                body["citations"][0]["delivery_ref"]["item_id"] = str(item_id)
            else:
                projected = next(entry for entry in items.values() if entry.item.content_type == "result_guideline_usage")
                body = projected.body.model_dump(mode="json")
                binding = next(binding for binding in body["bindings"] if binding["target"]["kind"] == "cell")
                binding["target"]["window_id"] = "another-window"
                content_type = "result_guideline_usage"
            identity = TranscriptItemIdentity(item_id=item_id, run_id=state.task.task.run_id,
                session_id=state.task.task.session_id, operation_id=usage.body.operation_id if content_type == "guideline_usage" else None,
                transcript_sequence=max(item.transcript_sequence for item in buffer.items)+1,
                created_at=state.app.state.m6_runtime.runner._clock.now())
            with pytest.raises(ValueError):
                buffer.append(identity, content_type, 1, body)
    asyncio.run(exercise())


def test_historical_case_closure_accepts_usage_refs_without_indexing_knowledge(monkeypatch):
    async def exercise():
        async with scenario(monkeypatch) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED
            snapshot, items = await records(state, ReadView.HISTORY)
            # This fixture holds the run open; source eligibility below uses a completed immutable snapshot.
            snapshot = snapshot.model_copy(update={"run": snapshot.run.model_copy(update={"status": RunStatus.COMPLETED})})
            root = next(entry for entry in items.values() if entry.item.content_type == "inference_result")

            class Reader:
                async def read_registered_run(self, run_id, view):
                    assert run_id == state.task.task.run_id and view is ReadView.HISTORY
                    return snapshot

            source_reader = TranscriptRAGCaseSourceReader(Reader(), state.app.state.run_coordinator._registry)
            job = SimpleNamespace(source_run_id=str(state.task.task.run_id), source_session_id=str(state.task.task.session_id),
                                  source_result_item_id=str(root.item.item_id))
            loaded = await source_reader.load_case_source(job)
            projection = build_prepared_projection(loaded, generation_id=str(uuid4()), profile_id="test-profile")
            assert projection.case.result_projection.overall_confidence == outcome.result.overall_confidence
            serialized = canonical_json_text(projection.case.model_dump(mode="json"))
            assert state.version.content.body_markdown not in serialized
            assert state.version.content.sources[0].url not in serialized
            assert all(entry.item.content_type == "evidence_fragment" for entry in loaded.fragments)
            sidecar = next(entry for entry in items.values() if entry.item.content_type == "result_guideline_usage")
            clean_detail = loaded.result.body.analysis_detail.model_copy(update={"transcript_item_refs": tuple(
                ref for ref in loaded.result.body.analysis_detail.transcript_item_refs if ref != str(sidecar.item.item_id))})
            clean_result = replace(loaded.result, body=loaded.result.body.model_copy(update={"analysis_detail": clean_detail}),
                                   references=tuple(ref for ref in loaded.result.references if ref != sidecar.item.item_id))
            clean = build_prepared_projection(replace(loaded, result=clean_result), generation_id=projection.case.case_ref.generation_id,
                                               profile_id="test-profile")
            assert clean.case.canonical_query == projection.case.canonical_query
            assert clean.case.result_projection == projection.case.result_projection
    asyncio.run(exercise())


def test_numeric_usage_writer_failure_propagates_out_of_the_dependency_schedule(monkeypatch):
    async def exercise():
        async with scenario(monkeypatch, model=AdvisoryModel(numeric=True), water_mode="empty") as state:
            coordinator = state.app.state.run_coordinator
            original = coordinator.finalize_transcript_item

            async def reject(run_id, item_id, content_type, *args, **kwargs):
                if content_type == "guideline_usage":
                    raise ValueError("synthetic numeric usage persistence failure")
                return await original(run_id, item_id, content_type, *args, **kwargs)

            monkeypatch.setattr(coordinator, "finalize_transcript_item", reject)
            with pytest.raises(GuidelineTranscriptWriteError):
                await state.runner.run(state.task, deadline=state.deadline)
            assert any(call.phase is WaterbodyModelPhase.NUMERIC_ESTIMATION for call in state.model.calls)
            assert not any(item.content.content_type == "inference_result"
                           for item in coordinator._runs[state.task.task.run_id].transcript_buffer.items)
    asyncio.run(exercise())


def test_cancellation_while_saving_retrieval_never_dispatches_or_records_delivery(monkeypatch):
    async def exercise():
        async with scenario(monkeypatch) as state:
            coordinator = state.app.state.run_coordinator
            entered, release = asyncio.Event(), asyncio.Event()
            original = coordinator.finalize_transcript_item

            async def pause(run_id, item_id, content_type, *args, **kwargs):
                if content_type == "guideline_retrieval":
                    entered.set()
                    await release.wait()
                return await original(run_id, item_id, content_type, *args, **kwargs)

            monkeypatch.setattr(coordinator, "finalize_transcript_item", pause)
            running = asyncio.create_task(state.runner.run(state.task, deadline=state.deadline))
            await asyncio.wait_for(entered.wait(), 5)
            state.task.task.cancellation_token._stop_requested.set()
            release.set()
            outcome = await asyncio.wait_for(running, 5)
            assert outcome.status is WaterbodyAgentRunStatus.CANCELLED
            assert not state.model.calls and not state.context.transcript.deliveries
            _, items = await records(state)
            assert not any(entry.item.content_type == "guideline_delivery" for entry in items.values())
    asyncio.run(exercise())


def test_repaired_output_usage_keeps_the_repair_operation_and_original_knowledge_subset(monkeypatch):
    class Model(AdvisoryModel):
        def _payload(self, request):
            if request.phase is WaterbodyModelPhase.SCHEMA_REPAIR:
                assert advisory(request) == self.original_block
                return self.corrected
            payload = super()._payload(request)
            if request.phase is WaterbodyModelPhase.REASONING_BATCH:
                self.corrected = json.loads(json.dumps(payload))
                self.original_block = advisory(request)
                payload["scope_digest"] = "sha256:incorrect"
            return payload

    async def exercise():
        async with scenario(monkeypatch, model=Model()) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            repaired = next(call for call in state.model.calls if call.phase is WaterbodyModelPhase.SCHEMA_REPAIR)
            _, items = await records(state)
            usage = next(entry for entry in items.values() if entry.item.content_type == "guideline_usage"
                         and entry.body.target.kind == "cell")
            assert usage.body.operation_id == repaired.operation_id and usage.body.phase == "schema_repair"
            assert usage.body.citations[0].phase == "schema_repair"
            assert len(state.context.transcript.deliveries) == len(state.model.calls) == 4
            root = next(entry for entry in items.values() if entry.item.content_type == "inference_result")
            validate_guideline_links(root.body, root.item, items.get)
    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["missing_delivery", "unknown_version"])
def test_single_content_history_read_hydrates_and_validates_its_bounded_reference_chain(monkeypatch, fault):
    from app.application.transcripts.reader import TranscriptContentUnreadableError

    async def exercise():
        settings = AppSettings(_env_file=None, log_level="warning", memory_enabled=False, transcript_inline_max_bytes=131072)
        async with scenario(monkeypatch, settings=settings) as state:
            outcome = await run_state(state)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED
            _, items = await records(state, ReadView.HISTORY)
            usage = next(entry for entry in items.values() if entry.item.content_type == "guideline_usage")
            root = next(entry for entry in items.values() if entry.item.content_type == "inference_result")
            delivery_id = usage.body.citations[0].delivery_ref.item_id
            body = usage.body.model_dump(mode="json")
            if fault == "unknown_version":
                body["citations"][0]["version_id"] = str(uuid4())
            serialized = canonical_json_text(body)
            reader = state.app.state.transcript_reader
            original = reader._store.read_run_items
            requested = []

            async def damaged(run_id, item_ids):
                requested.extend(item_ids)
                stored = await original(run_id, item_ids)
                entries = tuple(item.model_copy(update={"inline_body": serialized, "content_bytes": len(serialized.encode())})
                    if item.item_id == usage.item.item_id else item for item in stored.items
                    if not (fault == "missing_delivery" and item.item_id == delivery_id))
                return stored.model_copy(update={"items": entries})

            monkeypatch.setattr(reader._store, "read_run_items", damaged)
            with pytest.raises(TranscriptContentUnreadableError):
                await reader.get_content(state.task.task.run_id, usage.item.item_id, ReadView.HISTORY)
            assert delivery_id in requested
            assert root.item.item_id not in requested
    asyncio.run(exercise())
