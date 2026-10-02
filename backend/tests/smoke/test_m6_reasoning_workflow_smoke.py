import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.application.analysis_runs.models import AgentSessionStatus
from app.application.analysis_runs.persistence import PersistenceState
from app.application.reasoning import WaterbodyAgentRunStatus
from app.application.reasoning.model_models import (
    VisibleTextDelta,
    WaterbodyModelEvent,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.application.transcripts.read_models import ReadView
from app.infrastructure.config import AppSettings
from scripts.run_sse_smoke import read_event, running_server, stream_headers
from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort
from tests.m6_reasoning_support import (
    cancel_run,
    create_reasoning_test_app,
    prepare_waterbody,
    run_prepared,
)


M6_CONTENT_TYPES = {
    "waterbody_agent_input",
    "waterbody_model_request",
    "waterbody_agent_message",
    "waterbody_model_response",
    "reasoning_decision",
    "inference_result",
}


class VisibleAdaptiveWaterbodyModelPort(AdaptiveWaterbodyModelPort):
    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        first = True
        async for event in super().stream(request):
            if first:
                first = False
                yield VisibleTextDelta(
                    operation_id=request.operation_id,
                    model=request.model,
                    message_id=f"message:{request.operation_id}",
                    text="Visible fixture explanation. ",
                )
            yield event


class GatedVisibleModelPort(VisibleAdaptiveWaterbodyModelPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        self.started.set()
        await self.release.wait()
        async for event in super().stream(request):
            yield event


async def _wait_persisted(application, run_id) -> None:
    coordinator = application.state.run_coordinator
    runtime = coordinator._runs[run_id]
    async with asyncio.timeout(5):
        while (
            coordinator._transcript_writer.info_locked(runtime).state
            is not PersistenceState.UP_TO_DATE
        ):
            await asyncio.sleep(0)


async def _collect_session_events(client, path, *, last_event_id=None):
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else None
    events = []
    async with client.stream("GET", path, headers=headers) as response:
        stream_headers(response)
        lines = response.aiter_lines()
        while True:
            event = await read_event(lines)
            events.append(event)
            if event[0] == "stream_end":
                assert await anext(lines, None) is None
                return events


def test_complete_single_waterbody_uses_production_runner_writer_and_reader(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        model = VisibleAdaptiveWaterbodyModelPort()
        application, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=model,
        )
        async with application.router.lifespan_context(application):
            prepared = await prepare_waterbody(application, workflow, water)
            outcome = await run_prepared(application, prepared)

            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            assert outcome.result is not None
            result = outcome.result
            assert result.run_id == prepared.run_id
            assert result.session_id == prepared.session_id
            assert result.target_comid == "123"
            assert result.model_execution_summary.planning_calls == 1
            assert result.model_execution_summary.reasoning_batches == 1
            assert result.model_execution_summary.synthesis_calls == 1
            assert result.model_execution_summary.repair_calls == 0
            assert result.model_execution_summary.usage.input_tokens == 30
            assert result.series[0].points[0].outcome_status.value == "directional"
            assert result.series[0].points[0].assessment.risk_level.value == "medium"
            assert result.series[0].points[0].confidence == 0.7
            assert result.overall_assessment.availability.value == "complete"
            assert len(water.calls) == 2

            await _wait_persisted(application, prepared.run_id)
            reader = application.state.transcript_reader
            current = await reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.AUTO,
            )
            history = await reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.HISTORY,
            )
            assert [item.item_id for item in current.items] == [
                item.item_id for item in history.items
            ]
            m6_items = [
                item for item in current.items if item.content_type in M6_CONTENT_TYPES
            ]
            assert {item.content_type for item in m6_items} == M6_CONTENT_TYPES
            content_order = [item.content_type for item in m6_items]
            assert content_order[0] == "waterbody_agent_input"
            assert content_order[-1] == "inference_result"
            assert content_order.index("waterbody_model_request") < content_order.index(
                "waterbody_agent_message"
            ) < content_order.index("waterbody_model_response")
            assert content_order.index("waterbody_model_response") < content_order.index(
                "reasoning_decision"
            )
            assert all(item.parse_status.value == "ok" for item in m6_items)

            session = await reader.get_session(
                prepared.run_id,
                prepared.session_id,
                ReadView.HISTORY,
            )
            assert session.session.status is AgentSessionStatus.COMPLETED
            assert session.session.result_summary is not None
            summary = session.session.result_summary
            assert summary.result_id == result.result_id
            assert summary.risk_level is result.overall_assessment.risk_level
            assert summary.availability is result.overall_assessment.availability
            assert summary.overall_confidence == result.overall_confidence
            assert session.session.result_ref is not None
            assert session.session.result_ref.run_id == m6_items[-1].content_ref.run_id
            assert session.session.result_ref.item_id == m6_items[-1].content_ref.item_id
            assert result.analysis_detail.summary

            persistence = application.state.transcript_store
            terminal_batch = next(
                batch
                for batch in reversed(persistence.write_batches)
                if any(
                    mutation.summary.status is AgentSessionStatus.COMPLETED
                    for mutation in batch.session_mutations
                )
            )
            terminal_types = {
                item.item_id: item.content_type
                for item in terminal_batch.transcript.items
            }
            terminal_group = next(
                group
                for group in terminal_batch.groups
                if {
                    terminal_types[item_id] for item_id in group.item_ids
                } == {"inference_result", "lifecycle_event"}
            )
            assert len(terminal_group.mutation_ids) == 1

            public = json.dumps(
                current.model_dump(mode="json"),
                ensure_ascii=False,
            )
            assert "m6-fixture-secret" not in public
            assert "https://example.test/public?" not in public
            await cancel_run(application, prepared.run_id)

    asyncio.run(exercise())


def test_station_empty_and_late_model_failure_never_publish_partial_result(
    monkeypatch,
) -> None:
    async def exercise(mode, model):
        application, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=model,
            water_mode=mode,
        )
        async with application.router.lifespan_context(application):
            prepared = await prepare_waterbody(application, workflow, water)
            outcome = await run_prepared(application, prepared)
            transcript = await application.state.transcript_reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.AUTO,
            )
            session = await application.state.run_coordinator.get_session(
                prepared.run_id,
                prepared.session_id,
            )
            await cancel_run(application, prepared.run_id)
            return outcome, transcript, session

    async def scenarios() -> None:
        empty, empty_transcript, empty_session = await exercise(
            "empty",
            VisibleAdaptiveWaterbodyModelPort(),
        )
        assert empty.status is WaterbodyAgentRunStatus.COMPLETED
        assert empty.result is not None
        assert empty.result.overall_assessment.availability.value == "indeterminate"
        assert empty.result.model_execution_summary.planning_calls == 0
        assert empty.result.model_execution_summary.reasoning_batches == 0
        assert empty.result.model_execution_summary.synthesis_calls == 0
        assert empty.result.model_execution_summary.repair_calls == 0
        assert empty.result.model_execution_summary.usage is None
        assert empty_session.result_ref is not None

        failed, failed_transcript, failed_session = await exercise(
            "full",
            VisibleAdaptiveWaterbodyModelPort(
                fail_phase=WaterbodyModelPhase.OVERALL_SYNTHESIS,
            ),
        )
        assert failed.status is WaterbodyAgentRunStatus.FAILED
        assert failed.result is None
        assert failed.error is not None
        assert failed.error.code == "waterbody_model_timeout"
        assert failed_session.status is AgentSessionStatus.FAILED
        assert failed_session.result_ref is None
        assert failed_session.result_summary is None
        assert any(
            item.content_type == "reasoning_decision"
            for item in failed_transcript.items
        )
        assert all(
            item.content_type != "inference_result"
            for item in failed_transcript.items
        )

    asyncio.run(scenarios())


def test_cancel_after_final_checkpoint_wins_before_result_commit(monkeypatch) -> None:
    async def exercise() -> None:
        application, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=VisibleAdaptiveWaterbodyModelPort(),
            water_mode="empty",
        )
        async with application.router.lifespan_context(application):
            prepared = await prepare_waterbody(application, workflow, water)
            coordinator = application.state.run_coordinator
            commit_started = asyncio.Event()
            allow_commit = asyncio.Event()
            complete = coordinator.complete_waterbody_agent_session

            async def gated_complete(*args, **kwargs):
                commit_started.set()
                await allow_commit.wait()
                return await complete(*args, **kwargs)

            monkeypatch.setattr(
                coordinator,
                "complete_waterbody_agent_session",
                gated_complete,
            )
            runner = asyncio.create_task(run_prepared(application, prepared))
            await commit_started.wait()
            cancelled = await coordinator.cancel_agent_session(
                prepared.run_id,
                prepared.session_id,
            )
            allow_commit.set()
            outcome = await runner

            assert cancelled.status is AgentSessionStatus.CANCELLED
            assert outcome.status is WaterbodyAgentRunStatus.CANCELLED
            assert outcome.result is None
            assert outcome.error is None
            session = await coordinator.get_session(
                prepared.run_id,
                prepared.session_id,
            )
            assert session.status is AgentSessionStatus.CANCELLED
            assert session.result_ref is None
            assert session.result_summary is None
            transcript = await application.state.transcript_reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.AUTO,
            )
            assert all(
                item.content_type != "inference_result"
                for item in transcript.items
            )
            await cancel_run(application, prepared.run_id)

    asyncio.run(exercise())


def test_cancel_wins_before_failure_commit_without_transition_error(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        application, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=VisibleAdaptiveWaterbodyModelPort(
                fail_phase=WaterbodyModelPhase.EVIDENCE_PLANNING,
            ),
        )
        async with application.router.lifespan_context(application):
            prepared = await prepare_waterbody(application, workflow, water)
            coordinator = application.state.run_coordinator
            commit_started = asyncio.Event()
            allow_commit = asyncio.Event()
            fail = coordinator.fail_agent_session

            async def gated_fail(*args, **kwargs):
                commit_started.set()
                await allow_commit.wait()
                return await fail(*args, **kwargs)

            monkeypatch.setattr(coordinator, "fail_agent_session", gated_fail)
            runner = asyncio.create_task(run_prepared(application, prepared))
            await commit_started.wait()
            cancelled = await coordinator.cancel_agent_session(
                prepared.run_id,
                prepared.session_id,
            )
            allow_commit.set()
            outcome = await runner

            assert cancelled.status is AgentSessionStatus.CANCELLED
            assert outcome.status is WaterbodyAgentRunStatus.CANCELLED
            assert outcome.result is None
            assert outcome.error is None
            session = await coordinator.get_session(
                prepared.run_id,
                prepared.session_id,
            )
            assert session.status is AgentSessionStatus.CANCELLED
            assert session.result_ref is None
            assert session.result_summary is None
            await cancel_run(application, prepared.run_id)

    asyncio.run(exercise())


def test_session_sse_replay_reconnect_and_disconnect_do_not_cancel_runner(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        model = GatedVisibleModelPort()
        application, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=model,
            settings=AppSettings(
                _env_file=None,
                live_state_grace_seconds=0,
            ),
        )
        async with running_server(application) as client:
            prepared = await prepare_waterbody(application, workflow, water)
            runner = asyncio.create_task(run_prepared(application, prepared))
            await model.started.wait()
            path = (
                f"/api/analysis/runs/{prepared.run_id}/sessions/"
                f"{prepared.session_id}/events"
            )
            async with client.stream("GET", path) as response:
                stream_headers(response)
                first = await read_event(response.aiter_lines())
                assert first[0] == "transcript_item"

            assert not runner.done()
            assert not prepared.task.task.cancellation_token.is_requested()
            model.release.set()
            outcome = await runner
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED

            events = await _collect_session_events(client, path)
            names = [event[0] for event in events]
            assert names[-1] == "stream_end"
            assert "message_started" in names
            assert "message_delta" in names
            assert "message_finalized" in names
            assert names.index("message_started") < names.index(
                "message_delta"
            ) < names.index("message_finalized")
            ids = [event[2] for event in events if event[2] is not None]
            assert ids == sorted(set(ids))

            pivot = ids[len(ids) // 2]
            replay = await _collect_session_events(
                client,
                path,
                last_event_id=pivot,
            )
            replay_ids = [event[2] for event in replay if event[2] is not None]
            assert replay_ids
            assert all(event_id > pivot for event_id in replay_ids)
            assert len(replay_ids) == len(set(replay_ids))

            transcript = (
                await client.get(
                    f"/api/analysis/runs/{prepared.run_id}/sessions/"
                    f"{prepared.session_id}/transcript"
                )
            )
            assert transcript.status_code == 200
            transcript_json = transcript.json()
            finalized_texts = [
                item["content"]["body"]["text"]
                for item in transcript_json["items"]
                if item["content_type"] == "waterbody_agent_message"
                and item["content"]["kind"] == "inline"
            ]
            assert finalized_texts
            assert all(text == "Visible fixture explanation. " for text in finalized_texts)
            assert "m6-fixture-secret" not in json.dumps(transcript_json)

            run_path = f"/api/analysis/runs/{prepared.run_id}/events"
            run_stream = asyncio.create_task(
                _collect_session_events(client, run_path)
            )
            await asyncio.sleep(0.05)
            cancelled = await client.post(
                f"/api/analysis/runs/{prepared.run_id}/cancel"
            )
            assert cancelled.status_code == 200
            run_events = await run_stream
            serialized = json.dumps(run_events, ensure_ascii=False)
            for forbidden in (
                "Visible fixture explanation",
                "risk_rationale",
                '"series"',
                "evidence_refs",
                "m6-fixture-secret",
            ):
                assert forbidden not in serialized

            async with asyncio.timeout(5):
                while (
                    await application.state.run_coordinator.get_live_read(
                        prepared.run_id
                    )
                    is not None
                ):
                    await asyncio.sleep(0.01)
            history_stream = await client.get(path)
            assert history_stream.status_code == 410
            assert history_stream.json()["history_url"].endswith(
                f"/api/analysis/runs/{prepared.run_id}/sessions/"
                f"{prepared.session_id}/transcript?view=history"
            )

    asyncio.run(asyncio.wait_for(exercise(), timeout=45))
