import json
from datetime import timedelta
from uuid import UUID

import pytest

from app.application.run_summary.models import (
    RunSummaryAgentMessage,
    RunSummaryFinishReason,
    RunSummaryModelResponse,
)
from app.application.run_summary.prompt_builder import RunSummaryPromptBuilder
from app.application.run_summary.validator import RunSummaryValidator
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    ParseStatus,
    StoredTranscriptItem,
    TranscriptContentRef,
)
from app.application.transcripts.registry import (
    UnsupportedTranscriptContentError,
    create_production_registry,
)
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
