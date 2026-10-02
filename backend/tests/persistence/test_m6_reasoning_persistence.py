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
    WaterbodyModelRequest,
)
from app.application.transcripts.read_models import ReadSource, ReadView
from app.domain import InferenceResult
from app.infrastructure.config import AppSettings
from scripts.test_database import temporary_migrated_schema
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
                    text="Visible persisted fixture explanation. ",
                )
            yield event


async def _wait_persisted(application, run_id) -> None:
    coordinator = application.state.run_coordinator
    runtime = coordinator._runs[run_id]
    async with asyncio.timeout(45):
        while (
            coordinator._transcript_writer.info_locked(runtime).state
            is not PersistenceState.UP_TO_DATE
        ):
            await asyncio.sleep(0.01)


def test_full_m6_chain_survives_postgres_restart_with_typed_content(
    monkeypatch,
) -> None:
    async def exercise(base_settings: AppSettings) -> None:
        settings = base_settings.model_copy(
            update={
                "log_level": "warning",
                "db_operation_timeout_seconds": 30,
                "transcript_inline_max_bytes": 768,
                "transcript_preview_bytes": 80,
            }
        )
        first_app, workflow, water = create_reasoning_test_app(
            monkeypatch,
            model_port=VisibleAdaptiveWaterbodyModelPort(),
            settings=settings,
            transcript_store=None,
        )
        async with first_app.router.lifespan_context(first_app):
            prepared = await prepare_waterbody(first_app, workflow, water)
            outcome = await run_prepared(first_app, prepared)
            assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
            assert outcome.result is not None
            result = outcome.result
            await _wait_persisted(first_app, prepared.run_id)

            live = await first_app.state.transcript_reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.AUTO,
            )
            m6_live = [
                item for item in live.items if item.content_type in M6_CONTENT_TYPES
            ]
            assert {item.content_type for item in m6_live} == M6_CONTENT_TYPES
            message_items = [
                item
                for item in m6_live
                if item.content_type == "waterbody_agent_message"
            ]
            assert message_items
            assert all(item.content.kind == "inline" for item in message_items)
            result_item = next(
                item for item in m6_live if item.content_type == "inference_result"
            )
            assert result_item.content.kind == "reference"

            await cancel_run(first_app, prepared.run_id)
            await _wait_persisted(first_app, prepared.run_id)

        second_app, _, _ = create_reasoning_test_app(
            monkeypatch,
            model_port=VisibleAdaptiveWaterbodyModelPort(),
            settings=settings,
            transcript_store=None,
        )
        async with second_app.router.lifespan_context(second_app):
            reader = second_app.state.transcript_reader
            restored = await reader.get_session_transcript(
                prepared.run_id,
                prepared.session_id,
                ReadView.HISTORY,
            )
            assert restored.source is ReadSource.POSTGRESQL
            m6_restored = [
                item
                for item in restored.items
                if item.content_type in M6_CONTENT_TYPES
            ]
            assert [item.item_id for item in m6_restored] == [
                item.item_id for item in m6_live
            ]
            assert all(item.parse_status.value == "ok" for item in m6_restored)

            restored_result_item = next(
                item
                for item in m6_restored
                if item.content_type == "inference_result"
            )
            assert restored_result_item.content.kind == "reference"
            full = await reader.get_content(
                prepared.run_id,
                restored_result_item.item_id,
                ReadView.HISTORY,
            )
            restored_result = InferenceResult.model_validate_json(full.text)
            assert restored_result == result
            assert "m6-fixture-secret" not in full.text

            session = await reader.get_session(
                prepared.run_id,
                prepared.session_id,
                ReadView.HISTORY,
            )
            assert session.source is ReadSource.POSTGRESQL
            assert session.session.status is AgentSessionStatus.COMPLETED
            assert session.session.result_ref is not None
            assert session.session.result_ref.item_id == restored_result_item.item_id
            assert session.session.result_summary is not None
            assert session.session.result_summary.result_id == result.result_id

            transport = httpx.ASGITransport(app=second_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                transcript_path = (
                    f"/api/analysis/runs/{prepared.run_id}/sessions/"
                    f"{prepared.session_id}/transcript"
                )
                response = await client.get(transcript_path, params={"view": "history"})
                assert response.status_code == 200
                public = json.dumps(response.json(), ensure_ascii=False)
                assert "m6-fixture-secret" not in public

                stream = await client.get(
                    f"/api/analysis/runs/{prepared.run_id}/sessions/"
                    f"{prepared.session_id}/events"
                )
                assert stream.status_code == 410
                assert stream.json()["history_url"].endswith(
                    f"{transcript_path}?view=history"
                )

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))
