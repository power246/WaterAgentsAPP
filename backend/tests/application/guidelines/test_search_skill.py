import asyncio
import json
from uuid import uuid4

import pytest

from app.application.reasoning.model_models import ToolCallCompleted, StructuredOutputCompleted, Terminal, UsageReported
from app.domain.guidelines import CanonicalGuidelineDocumentV1, CanonicalGuidelineDocumentV2, canonical_guideline_document
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.adapters.openai_responses_waterbody import _request_body
from tests.application.guidelines.test_model_context import real_context, model_context, advisory, token
from tests.application.guidelines.test_retrieval import Repository
from tests.application.reasoning.test_numeric_estimation import _estimator, EstimatePort
from tests.guideline_support import profile, content


def model_request():
    from tests.infrastructure.test_openai_responses_waterbody import model_request as sample
    request = sample()
    return request.model_copy(update={"messages": (request.messages[0].model_copy(update={"text": '{"task":"Explain pH changes under the supplied bounds"}'}),)})


class Lookup:
    def __init__(self, *, query="pH photosynthesis respiration carbon dioxide acid-base balance", name="search_guidelines", skip=False, extra=None, twice=False, plain=False):
        self.calls = []
        self.query, self.name, self.skip, self.extra, self.twice = query, name, skip, extra, twice
        self.plain = plain

    async def stream(self, request):
        self.calls.append(request)
        values = {"operation_id": request.operation_id, "model": request.model}
        if self.skip or self.plain:
            yield StructuredOutputCompleted(**values, output_id="decision",
                value=json.dumps({"query": self.query} if self.plain else {"decision": "skip"}))
        else:
            for index in range(2 if self.twice else 1):
                yield ToolCallCompleted(**values, provider_call_id=f"call:{index}", name=self.name,
                    arguments=json.dumps({"query": self.query, "aspects": [self.query], **(self.extra or {})}))
        yield UsageReported(**values, input_tokens=13, output_tokens=9)
        yield Terminal(**values, provider_response_id="guideline-search-response", finish_reason="completed")


def test_model_rewrite_is_the_actual_embedding_question_and_scope_stays_pinned():
    async def exercise():
        session, embedding, repository = await real_context()
        port = Lookup()
        context = model_context(session, _estimator(EstimatePort())._model)
        request = model_request()
        before = session._query
        try:
            prepared = await context.prepare(request, cancellation=token(), model_port=port)
            assert len(port.calls) == len(embedding.calls) == len(repository.ranks) == 1
            lookup = port.calls[0]
            assert lookup.operation_id != request.operation_id
            assert json.loads(lookup.messages[-1].text)["original_question"] == session.original_question
            assert [d.name for d in lookup.tool_descriptors] == ["search_guidelines"]
            query = embedding.calls[0][1][0]
            assert query.question == port.query
            assert query.parameter_keys == before.parameter_keys and query.profile_id == before.profile_id
            assert query.mode == before.mode and query.task_facts == before.task_facts
            assert repository.ranks[0][0] == session.pin.catalog
            assert repository.ranks[0][3] == {"cutoff": .35, "limit": 3}
            assert advisory(prepared)[0]["body"] == repository.matches[0].version.model_view.body
            wire = _request_body(prepared)
            assert wire["input"][-3]["type"] == "function_call"
            assert wire["input"][-2]["type"] == "function_call_output"
            assert json.loads(wire["input"][-2]["output"])["query"] == port.query
            second = await context.prepare(request.model_copy(update={"operation_id": uuid4()}), cancellation=token(), model_port=port)
            assert advisory(second) and len(port.calls) == len(embedding.calls) == 1
            assert context.search_calls == context.search_tool_calls == 1
            assert context.search_usage.input_tokens == 13
        finally:
            await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("options", [
    {"skip": True}, {"query": " "}, {"query": "x" * 401}, {"query": "https://invalid.test/"},
    {"name": "read_database"}, {"extra": {"top_k": 100}}, {"extra": {"profile_id": "forged"}},
    {"extra": {"parameter_keys": []}}, {"twice": True}, {"plain": True},
])
def test_skip_or_invalid_tool_never_embeds_or_retries(options):
    async def exercise():
        session, embedding, repository = await real_context()
        port = Lookup(**options)
        context = model_context(session, model_request().model)
        try:
            request = model_request()
            prepared = await context.prepare(request, cancellation=token(), model_port=port)
            await context.prepare(request.model_copy(update={"operation_id": uuid4()}), cancellation=token(), model_port=port)
            assert len(port.calls) == 1 and not embedding.calls and not repository.ranks
            assert not advisory(prepared)
            assert context._selection.status == ("no_match" if options.get("skip") else "unavailable")
        finally:
            await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("library,candidates", [(0, 0), (1, 0)])
def test_empty_catalog_has_no_added_model_call(library, candidates):
    async def exercise():
        session, embedding, _ = await real_context(repository=Repository(library, candidates))
        request, port = model_request(), Lookup()
        context = model_context(session, request.model)
        try:
            await context.prepare(request, cancellation=token(), model_port=port)
            assert not port.calls and not embedding.calls and context.search_calls == 0
        finally:
            await session.aclose()
    asyncio.run(exercise())


def test_summary_v2_excludes_body_and_legacy_profile_retains_its_exact_contract():
    registry = load_parameter_capability_registry()
    legacy = profile(text_contract_version=1, document_template_version=1)
    current = profile()
    document = content(body_markdown="Full scientific conditions and exceptions stay in the immutable original.")
    old, new = (canonical_guideline_document(document, p, registry) for p in (legacy, current))
    assert isinstance(old, CanonicalGuidelineDocumentV1) and isinstance(new, CanonicalGuidelineDocumentV2)
    assert old.profile_id != new.profile_id
    assert document.body_markdown in old.canonical_text and document.body_markdown not in new.canonical_text
    assert new.title == document.title and new.summary == document.summary
    assert canonical_guideline_document(content(body_markdown="A different original body."), current, registry) == new
    for p, doc in ((legacy, old), (current, new)):
        doc.require_profile(p)
    with pytest.raises(ValueError):
        old.require_profile(current)


def test_all_four_approved_topics_fit_google_wire_gate_without_body_changes():
    from scripts.guideline_evaluation import reviewed_drafts
    from app.application.guidelines.embedding import validated_guideline_texts
    from app.domain.guidelines import GuidelinePolicy, GuidelineEmbeddingProfileV1
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "docs/guidelines/ph_guideline_drafts.json"
    before = path.read_bytes()
    _, topics = reviewed_drafts(path)
    policy = GuidelinePolicy()
    selected = GuidelineEmbeddingProfileV1(provider="openai", model="gemini-embedding-001",
        output_dimensions=1536, max_input_tokens=2048, document_instructions=policy.document_instructions,
        query_instructions=policy.query_instructions)
    for topic in topics:
        document = canonical_guideline_document(topic["content"], selected, load_parameter_capability_registry())
        wires = validated_guideline_texts(selected, document.fragments)
        assert len(wires) > 1
        assert all(len(wire.encode()) <= 2048 and topic["content"].body_markdown not in wire for wire in wires)
    assert path.read_bytes() == before
    assert json.loads(before)["status"] == "approved_for_bundled_distribution"


def test_unprepared_model_request_cannot_bypass_guideline_delivery_recording():
    from types import SimpleNamespace
    from app.application.guidelines.transcript import GuidelineTranscriptWriteError
    from app.application.reasoning.transcript_store import TranscriptRecordingWaterbodyModelPort
    from tests.application.guidelines.test_model_context import Session

    async def exercise():
        request, delegate = model_request(), Lookup()
        context = model_context(Session(()), request.model)
        writes = []

        async def write(actual):
            writes.append(actual)

        store = SimpleNamespace(guidelines=context, _cancellation=token(), write_model_request=write)
        port = TranscriptRecordingWaterbodyModelPort(delegate=delegate, store=store)
        with pytest.raises(GuidelineTranscriptWriteError):
            async for _ in port.stream(request):
                pass
        assert not writes and not delegate.calls
    asyncio.run(exercise())
