import asyncio
import json
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionSummary,
)
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
    StoredTranscriptContent,
    StoredTranscriptItem,
    TranscriptContentRef,
    TranscriptItem,
    WorkflowQuiescedFact,
)
from app.application.transcripts.read_models import (
    ReadView,
    StoredRunHeader,
    StoredRunRead,
)
from app.application.transcripts.reader import (
    TranscriptItemNotFoundError,
    TranscriptReader,
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

        async def read_run_items(self, run_id, item_ids):
            stored = await self.read_run(run_id)
            return stored.model_copy(update={
                "items": tuple(
                    item for item in stored.items if item.item_id in item_ids
                ),
            })

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


def test_history_content_and_session_reads_are_bounded_to_referenced_items(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        registry = create_production_registry()
        input_id = UUID("c5a64a52-c1e2-4725-88ee-6c83f105e635")
        unrelated_id = UUID("d5a64a52-c1e2-4725-88ee-6c83f105e636")
        other_session_id = UUID("e5a64a52-c1e2-4725-88ee-6c83f105e637")
        request = RunSummaryPromptBuilder().build(
            summary_input(),
            model=MODEL,
            input_ref=TranscriptContentRef(run_id=RUN_ID, item_id=input_id),
            operation_id=UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633"),
            deadline=NOW + timedelta(minutes=1),
        )
        request_item = _stored(
            "run_summary_model_request",
            registry.encode("run_summary_model_request", 1, request).safe_body,
        )
        input_body = registry.encode(
            "run_summary_input", 1, summary_input()
        ).safe_body
        input_item = _stored("run_summary_input", input_body).model_copy(update={
            "item_id": input_id,
            "session_id": other_session_id,
            "transcript_sequence": 2,
            "inline_body": None,
            "preview_head": "Input preview",
            "preview_tail": "Input preview",
        })
        unrelated = _stored("runtime_fact", '{"blob":"' + "x" * 100_000 + '"}').model_copy(
            update={
                "item_id": unrelated_id,
                "session_id": other_session_id,
                "transcript_sequence": 3,
            }
        )
        header = StoredRunHeader(
            run_id=RUN_ID,
            status=RunStatus.COMPLETED,
            created_at=NOW,
            updated_at=NOW,
            query="Summarize COMID 300.",
            request_item_id=INPUT_ITEM_ID,
            created_item_id=INPUT_ITEM_ID,
        )
        sessions = (
            SessionSummary(
                session_id=SUMMARY_SESSION_ID,
                agent_role=AgentRole.RUN_SUMMARIZER,
                status=AgentSessionStatus.RUNNING,
                created_at=NOW,
                updated_at=NOW,
            ),
            SessionSummary(
                session_id=other_session_id,
                agent_role=AgentRole.WATERBODY,
                target_comid="12345678",
                status=AgentSessionStatus.RUNNING,
                created_at=NOW,
                updated_at=NOW,
            ),
        )
        all_items = (request_item, input_item, unrelated)
        read_batches: list[tuple[UUID, ...]] = []

        class Store:
            async def read_run(self, run_id):
                raise AssertionError(f"full-run read attempted for {run_id}")

            async def read_run_detail_roots(self, run_id):
                return StoredRunRead(run=header, sessions=sessions) if run_id == RUN_ID else None

            async def list_session_item_ids(self, run_id, session_id, content_type):
                assert run_id == RUN_ID and content_type is None
                return tuple(
                    item.item_id for item in all_items
                    if item.session_id == session_id
                )

            async def read_run_items(self, run_id, item_ids):
                assert run_id == RUN_ID
                read_batches.append(item_ids)
                return StoredRunRead(
                    run=header,
                    items=tuple(
                        item for item in all_items if item.item_id in item_ids
                    ),
                    contents=(
                        StoredTranscriptContent(
                            item_id=input_id, body=input_body, created_at=NOW,
                        ),
                    ) if input_id in item_ids else (),
                )

        class Live:
            async def get_live_read(self, run_id):
                del run_id

        reader = TranscriptReader(
            Live(), Store(), registry,
            error_id_factory=lambda: RUN_ID,
            inline_content_bytes=200_000,
            preview_bytes=1_024,
        )
        session = await reader.get_session(
            RUN_ID, SUMMARY_SESSION_ID, ReadView.HISTORY
        )
        assert session.session.session_id == SUMMARY_SESSION_ID
        assert read_batches == []
        content = await reader.get_content(
            RUN_ID, request_item.item_id, ReadView.HISTORY
        )
        assert content.item_id == request_item.item_id
        assert read_batches == [(request_item.item_id,)]
        read_batches.clear()
        parse_calls: Counter[UUID] = Counter()
        original_parse = reader._parse_item

        def count_parse(stored, item):
            parse_calls[item.item_id] += 1
            return original_parse(stored, item)

        monkeypatch.setattr(reader, "_parse_item", count_parse)
        transcript = await reader.get_session_transcript(
            RUN_ID, SUMMARY_SESSION_ID, ReadView.HISTORY
        )
        assert tuple(item.item_id for item in transcript.items) == (
            request_item.item_id,
        )
        assert tuple(item.item_id for item in transcript.referenced_items) == (
            input_id,
        )
        assert transcript.referenced_items[0].parse_status is ParseStatus.OK
        assert parse_calls == Counter({request_item.item_id: 1, input_id: 1})
        assert read_batches[:2] == [(request_item.item_id,), (input_id,)]
        assert unrelated_id not in {
            item_id for batch in read_batches for item_id in batch
        }
        read_batches.clear()
        with pytest.raises(TranscriptItemNotFoundError):
            await reader.get_content(
                RUN_ID, request_item.item_id, ReadView.AUTO
            )
        auto_transcript = await reader.get_session_transcript(
            RUN_ID, SUMMARY_SESSION_ID, ReadView.AUTO
        )
        assert auto_transcript.items == ()
        assert auto_transcript.referenced_items == ()
        assert read_batches == [
            (request_item.item_id,), (request_item.item_id,)
        ]

    asyncio.run(exercise())


def test_run_detail_parses_each_item_once_across_large_reference_waves(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        registry = create_production_registry()
        body = registry.encode("runtime_fact", 1, WorkflowQuiescedFact()).safe_body
        item_ids = tuple(UUID(int=index + 1) for index in range(1_000))
        items = tuple(
            _stored("runtime_fact", body).model_copy(update={
                "item_id": item_id,
                "session_id": None if index == 0 else SUMMARY_SESSION_ID,
                "transcript_sequence": index + 1,
            })
            for index, item_id in enumerate(item_ids)
        )
        by_id = {item.item_id: item for item in items}
        references = {
            item_ids[0]: (item_ids[1],),
            item_ids[1]: (item_ids[2],),
            item_ids[2]: item_ids[3:],
        }
        original_references = registry.references_from_read

        def item_references(item):
            if item.item.item_id in references:
                return references[item.item.item_id]
            return original_references(item)

        monkeypatch.setattr(registry, "references_from_read", item_references)
        header = StoredRunHeader(
            run_id=RUN_ID,
            status=RunStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
            query="Analyze the waterbody.",
            request_item_id=item_ids[0],
            created_item_id=item_ids[0],
        )
        session = SessionSummary(
            session_id=SUMMARY_SESSION_ID,
            agent_role=AgentRole.WATERBODY,
            target_comid="12345678",
            status=AgentSessionStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        read_batches: list[tuple[UUID, ...]] = []

        class Store:
            async def read_run_detail_roots(self, run_id):
                return (
                    StoredRunRead(run=header, sessions=(session,), items=(items[0],))
                    if run_id == RUN_ID else None
                )

            async def read_run_items(self, run_id, requested):
                assert run_id == RUN_ID
                read_batches.append(requested)
                return StoredRunRead(
                    run=header,
                    items=tuple(by_id[item_id] for item_id in requested),
                )

        class Live:
            async def get_live_read(self, run_id):
                del run_id

        reader = TranscriptReader(
            Live(), Store(), registry,
            error_id_factory=lambda: RUN_ID,
            inline_content_bytes=65_536,
            preview_bytes=2_048,
        )
        parse_calls: Counter[UUID] = Counter()
        original_parse = reader._parse_item

        def count_parse(stored, item):
            parse_calls[item.item_id] += 1
            return original_parse(stored, item)

        monkeypatch.setattr(reader, "_parse_item", count_parse)
        for view in (ReadView.AUTO, ReadView.HISTORY):
            detail = await reader.get_run(RUN_ID, view)
            assert tuple(item.item_id for item in detail.items) == item_ids[:1]
            assert {item.item_id for item in detail.referenced_items} == set(item_ids[1:])
            assert len(detail.referenced_items) == 999
            assert parse_calls == Counter({item_id: 1 for item_id in item_ids})
            assert tuple(len(batch) for batch in read_batches) == (1, 1, 997)
            parse_calls.clear()
            read_batches.clear()

    asyncio.run(exercise())


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

    original_parse = registry.parse
    parse_calls = 0

    def count_parse(item, body, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(item, body, **kwargs)

    monkeypatch.setattr(registry, "parse", count_parse)
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
    assert parse_calls == 1
