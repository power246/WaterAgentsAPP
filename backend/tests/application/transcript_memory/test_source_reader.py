import asyncio
from dataclasses import replace

import pytest

from app.application.transcript_memory import RAGSourceReadError, TypedRAGSourceReader
from app.application.transcripts.models import (
    ParseStatus,
    ReadTranscriptItem,
    TranscriptContentRef,
)
from app.application.transcripts.read_models import ReadView
from tests.application.transcript_memory.m3_support import REGISTRY, case_source


class SingleItemReader:
    def __init__(self, read: ReadTranscriptItem) -> None:
        self.read = read
        self.calls = []

    async def read_registered_item(
        self,
        run_id,
        item_id,
        view,
        *,
        session_id=None,
    ) -> ReadTranscriptItem:
        self.calls.append((run_id, item_id, view, session_id))
        return self.read


def _read(typed, *, body=None, item=None) -> ReadTranscriptItem:
    stored = item or typed.item
    return ReadTranscriptItem(
        item=stored,
        content_ref=TranscriptContentRef(
            run_id=stored.run_id,
            item_id=stored.item_id,
        ),
        parse_status=ParseStatus.OK,
        body=body or typed.body,
    )


def test_typed_reader_reads_exactly_one_registered_history_item() -> None:
    source = case_source()
    backend = SingleItemReader(_read(source.result))
    reader = TypedRAGSourceReader(backend, REGISTRY)

    result = asyncio.run(
        reader.read_result(
            source.result.item.run_id,
            source.session.session_id,
            source.result.item.item_id,
        )
    )

    assert result.body == source.result.body
    assert result.references == source.result.references
    assert backend.calls == [
        (
            source.result.item.run_id,
            source.result.item.item_id,
            ReadView.HISTORY,
            source.session.session_id,
        )
    ]


@pytest.mark.parametrize(
    ("item_changes", "expected"),
    (
        ({"content_schema_version": 2}, "historical_source_schema_unsupported"),
        ({"content_type": "unknown_content"}, "historical_source_schema_unsupported"),
        ({"rag_eligible": False}, "historical_source_not_rag_candidate"),
    ),
)
def test_typed_reader_rejects_unknown_schema_and_non_candidate_without_fallback(
    item_changes,
    expected,
) -> None:
    source = case_source()
    item = source.result.item.model_copy(update=item_changes)
    backend = SingleItemReader(_read(source.result, item=item))
    reader = TypedRAGSourceReader(backend, REGISTRY)

    with pytest.raises(RAGSourceReadError, match=expected):
        asyncio.run(
            reader.read_result(
                item.run_id,
                source.session.session_id,
                item.item_id,
            )
        )

    assert len(backend.calls) == 1


def test_typed_reader_rejects_wrong_typed_body_and_scope() -> None:
    source = case_source()
    wrong_body_backend = SingleItemReader(
        _read(source.result, body=source.analysis_context.body)
    )
    wrong_body_reader = TypedRAGSourceReader(wrong_body_backend, REGISTRY)
    scoped_context = replace(
        source.analysis_context,
        item=source.analysis_context.item.model_copy(
            update={"session_id": source.session.session_id}
        ),
    )
    scoped_backend = SingleItemReader(_read(scoped_context))
    scoped_reader = TypedRAGSourceReader(scoped_backend, REGISTRY)

    with pytest.raises(
        RAGSourceReadError,
        match="historical_source_schema_unsupported",
    ):
        asyncio.run(
            wrong_body_reader.read_result(
                source.result.item.run_id,
                source.session.session_id,
                source.result.item.item_id,
            )
        )
    with pytest.raises(RAGSourceReadError, match="historical_source_scope_mismatch"):
        asyncio.run(
            scoped_reader.read_analysis_context(
                scoped_context.item.run_id,
                scoped_context.item.item_id,
            )
        )

    assert len(wrong_body_backend.calls) == 1
    assert len(scoped_backend.calls) == 1
