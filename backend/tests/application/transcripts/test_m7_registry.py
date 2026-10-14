import asyncio
import json
from types import SimpleNamespace
from datetime import timedelta
from uuid import UUID

import pytest

from app.application.analysis_runs.models import RunStatus
from app.application.run_summary.models import (
    RunSummaryAgentMessage,
    RunSummaryFinishReason,
    RunSummaryModelResponse,
)
from app.application.run_summary.prompt_builder import RunSummaryPromptBuilder
from app.application.run_summary.validator import RunSummaryValidator
from app.application.transcripts.models import (
    ContentVisibility,
    GenerationStatus,
    MessageEndReason,
    ParseStatus,
    StoredTranscriptItem,
    TranscriptItem,
    TranscriptContentRef,
)
from app.application.transcripts.registry import (
    UnsupportedTranscriptContentError,
    create_production_registry,
)
from app.application.transcripts.read_models import (
    ReadView,
    StoredRunHeader,
    StoredRunRead,
)
from app.application.transcripts.reader import TranscriptReader
from tests.application.analysis_runs.test_topology_result_models import (
    _manifest,
    _topology_result,
)
from tests.application.run_summary.support import (
    INPUT_ITEM_ID,
    MODEL,
    NOW,
    RUN_ID,
    SUMMARY_SESSION_ID,
    draft_payload,
    summary_input,
)


def _stored(content_type: str, body: str) -> StoredTranscriptItem:
    return StoredTranscriptItem(
        item_id=UUID("95a64a52-c1e2-4725-88ee-6c83f105e632"),
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        transcript_sequence=1,
        created_at=NOW,
        content_type=content_type,
        content_schema_version=1,
        inline_body=body,
        content_bytes=len(body.encode("utf-8")),
    )


def test_all_seven_m7_v1_contents_round_trip_with_reference_extractors() -> None:
    registry = create_production_registry()
    summary = summary_input()
    input_ref = TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID)
    request = RunSummaryPromptBuilder().build(
        summary,
        model=MODEL,
        input_ref=input_ref,
        operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        deadline=NOW + timedelta(minutes=1),
    )
    message_ref = TranscriptContentRef(
        run_id=RUN_ID,
        item_id=UUID("b5a64a52-c1e2-4725-88ee-6c83f105e634"),
    )
    message = RunSummaryAgentMessage(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        message_id="summary-message",
        operation_id=request.operation_id,
        text="Visible summary.",
        generation_status=GenerationStatus.COMPLETED,
        end_reason=MessageEndReason.COMPLETED,
    )
    response = RunSummaryModelResponse(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        operation_id=request.operation_id,
        model=MODEL,
        finish_reason=RunSummaryFinishReason.COMPLETED,
        message_refs=(message_ref,),
        structured_output=json.dumps(draft_payload()),
        visible_text="Visible summary.",
        provider_response_id="response-m7",
    )
    _, result = RunSummaryValidator().validate(
        summary,
        json.dumps(draft_payload()),
        model=MODEL,
        usage=None,
        generated_at=NOW,
    )
    samples = {
        "waterbody_dependency_manifest": _manifest(),
        "run_summary_input": summary,
        "run_summary_model_request": request,
        "run_summary_agent_message": message,
        "run_summary_model_response": response,
        "run_summary_result": result,
        "topology_run_result": _topology_result(),
    }

    for content_type, value in samples.items():
        encoded = registry.encode(content_type, 1, value)
        parsed = registry.parse(_stored(content_type, encoded.safe_body), encoded.safe_body)
        assert parsed.parse_status is ParseStatus.OK
        assert type(parsed.body) is type(value)
        assert encoded.references == registry.references_from_read(parsed)

    assert registry.encode("run_summary_agent_message", 1, message).references == ()
    assert registry.encode("run_summary_model_request", 1, request).references == (
        INPUT_ITEM_ID,
    )
    assert registry.encode("run_summary_model_response", 1, response).references == (
        message_ref.item_id,
    )
    assert registry.encode("run_summary_result", 1, result).references

    for content_type, raw, secrets in (
        (
            "run_summary_model_request",
            registry.encode("run_summary_model_request", 1, request).safe_body,
            (request.system_prompt, request.messages[0].text, "response_schema"),
        ),
        (
            "run_summary_model_response",
            registry.encode("run_summary_model_response", 1, response).safe_body,
            (response.structured_output, response.visible_text),
        ),
    ):
        parsed = registry.parse(_stored(content_type, raw), raw)
        public_text = registry.public_content_body(content_type, 1, raw)
        assert registry.visibility_from_read(parsed) is ContentVisibility.HISTORY_ONLY
        assert public_text == registry.public_safe_body(content_type, 1, raw)
        assert all(secret not in public_text for secret in secrets)

    projected_response = json.loads(
        registry.public_content_body(
            "run_summary_model_response",
            1,
            registry.encode("run_summary_model_response", 1, response).safe_body,
        )
    )
    assert projected_response["structured_output_present"] is True
    assert projected_response["visible_text_present"] is True


def test_summary_message_uses_the_existing_message_lifecycle_and_unknown_v2_fails() -> None:
    registry = create_production_registry()
    generating = RunSummaryAgentMessage(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        message_id="summary-message",
        operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        text="",
        generation_status=GenerationStatus.GENERATING,
    )
    encoded = registry.encode("run_summary_agent_message", 1, generating)
    projection = registry.message_projection(
        "run_summary_agent_message",
        1,
        encoded.safe_body,
    )
    finalized = registry.finalized_message_body(
        "run_summary_agent_message",
        1,
        "safe text",
        GenerationStatus.COMPLETED,
        MessageEndReason.COMPLETED,
        encoded.safe_body,
    )

    assert projection.generation_status is GenerationStatus.GENERATING
    assert finalized.text == "safe text"
    assert finalized.generation_status is GenerationStatus.COMPLETED
    with pytest.raises(UnsupportedTranscriptContentError):
        registry.encode("run_summary_result", 2, {})


def test_full_content_reader_applies_summary_request_projection() -> None:
    registry = create_production_registry()
    request = RunSummaryPromptBuilder().build(
        summary_input(),
        model=MODEL,
        input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        deadline=NOW + timedelta(minutes=1),
    )
    encoded = registry.encode("run_summary_model_request", 1, request)
    stored_item = _stored("run_summary_model_request", encoded.safe_body)

    class Store:
        async def read_run(self, run_id):
            assert run_id == RUN_ID
            return StoredRunRead(
                run=StoredRunHeader(
                    run_id=RUN_ID,
                    status=RunStatus.COMPLETED,
                    created_at=NOW,
                    updated_at=NOW,
                    query="Summarize COMID 300.",
                    request_item_id=INPUT_ITEM_ID,
                    created_item_id=UUID("b5a64a52-c1e2-4725-88ee-6c83f105e634"),
                ),
                items=(stored_item,),
            )

    reader = TranscriptReader(
        object(),
        Store(),
        registry,
        error_id_factory=lambda: RUN_ID,
        inline_content_bytes=64 * 1024,
        preview_bytes=1024,
    )
    content = asyncio.run(
        reader.get_content(RUN_ID, stored_item.item_id, ReadView.HISTORY)
    )

    assert request.system_prompt not in content.text
    assert request.messages[0].text not in content.text
    assert "response_schema" not in content.text
    assert content.content_bytes == len(content.text.encode("utf-8"))


def test_live_http_long_preview_uses_public_projection() -> None:
    registry = create_production_registry()
    summary = summary_input()
    request = RunSummaryPromptBuilder().build(
        summary,
        model=MODEL,
        input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        deadline=NOW + timedelta(minutes=1),
    )
    response = RunSummaryModelResponse(
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
        operation_id=request.operation_id,
        model=MODEL,
        finish_reason=RunSummaryFinishReason.COMPLETED,
        message_refs=(),
        structured_output=json.dumps({"internal": "INTERNAL_ONLY_MARKER" + "x" * 80_000}),
        visible_text="",
        provider_response_id="response-long",
    )
    content = registry.encode("run_summary_model_response", 1, response)
    header_content = registry.encode("run_summary_model_request", 1, request)
    items = (
        TranscriptItem(
            item_id=UUID(int=11), run_id=RUN_ID, transcript_sequence=1,
            created_at=NOW, content=header_content,
        ),
        TranscriptItem(
            item_id=UUID(int=12), run_id=RUN_ID, transcript_sequence=2,
            created_at=NOW, content=header_content,
        ),
        TranscriptItem(
            item_id=UUID(int=13), run_id=RUN_ID,
            session_id=SUMMARY_SESSION_ID, transcript_sequence=1,
            created_at=NOW, content=content,
        ),
    )
    live = SimpleNamespace(
        run=SimpleNamespace(
            run_id=RUN_ID, status=RunStatus.RUNNING,
            created_at=NOW, updated_at=NOW,
        ),
        query="Read a run.", sessions=(), items=items,
    )
    reader = TranscriptReader(
        object(), object(), registry,
        error_id_factory=lambda: RUN_ID,
        inline_content_bytes=1024,
        preview_bytes=1024,
    )

    stored = reader._stored_live(live)
    preview = next(item for item in stored.items if item.item_id == UUID(int=13))

    assert preview.inline_body is None
    assert "INTERNAL_ONLY_MARKER" not in preview.preview_head
    assert "INTERNAL_ONLY_MARKER" not in preview.preview_tail
    assert "structured_output_present" in preview.preview_head


def test_registered_item_only_parses_the_requested_item(monkeypatch) -> None:
    registry = create_production_registry()
    request = RunSummaryPromptBuilder().build(
        summary_input(),
        model=MODEL,
        input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=INPUT_ITEM_ID),
        operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
        deadline=NOW + timedelta(minutes=1),
    )
    encoded = registry.encode("run_summary_model_request", 1, request)
    unrelated = _stored(
        "run_summary_model_request",
        encoded.safe_body,
    ).model_copy(update={
        "item_id": UUID("b5a64a52-c1e2-4725-88ee-6c83f105e634"),
    })
    requested = unrelated.model_copy(update={
        "item_id": UUID("c5a64a52-c1e2-4725-88ee-6c83f105e635"),
        "transcript_sequence": 2,
    })

    class Store:
        async def read_run(self, run_id):
            assert run_id == RUN_ID
            return StoredRunRead(
                run=StoredRunHeader(
                    run_id=RUN_ID,
                    status=RunStatus.COMPLETED,
                    created_at=NOW,
                    updated_at=NOW,
                    query="Summarize COMID 300.",
                    request_item_id=INPUT_ITEM_ID,
                    created_item_id=unrelated.item_id,
                ),
                items=(unrelated, requested),
            )

    safe_read_body = registry.safe_read_body
    safe_read_calls = 0

    def count_safe_read(content_type, content_schema_version, body):
        nonlocal safe_read_calls
        safe_read_calls += 1
        return safe_read_body(content_type, content_schema_version, body)

    monkeypatch.setattr(registry, "safe_read_body", count_safe_read)
    reader = TranscriptReader(
        object(),
        Store(),
        registry,
        error_id_factory=lambda: RUN_ID,
        inline_content_bytes=64 * 1024,
        preview_bytes=1024,
    )

    item = asyncio.run(
        reader.read_registered_item(RUN_ID, requested.item_id, ReadView.HISTORY)
    )

    assert item.item.item_id == requested.item_id
    assert item.body == request
    assert safe_read_calls == 1
