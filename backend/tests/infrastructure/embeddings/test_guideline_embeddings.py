import asyncio
import json
import logging

import httpx
import pytest

from app.domain.guidelines import CanonicalGuidelineDocumentV1
from app.infrastructure.adapters import OpenAIEmbeddingAdapter
from app.infrastructure.adapters.openai_guideline_embeddings import OpenAIGuidelineEmbeddingAdapter
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.http import ProviderHttpClient
from app.ports import EmbeddingCancelledError, InvalidPortResponseError, PortConfigurationError, PortHttpError, PortTimeoutError
from tests.guideline_support import content, profile, query
from tests.infrastructure.embeddings.support import canonical_text, embedding_profile
from tests.infrastructure.embeddings.test_openai_embeddings import http_client, response_body


def test_document_and_query_share_transport_with_exact_roles_and_safe_payload():
    selected = profile(max_input_tokens=10_000, document_instructions="passage: ", query_instructions="query: ")
    inputs = (CanonicalGuidelineDocumentV1.from_content(content(), selected, load_parameter_capability_registry()), query(selected))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        return httpx.Response(200, json=response_body(selected.model, [[1, 0, 0], [0, 1, 0]]))

    async def exercise():
        async with http_client(handler) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
            assert (await adapter.embed(selected, (), None, None)).vectors == ()
            batch = await adapter.embed(selected, inputs, None, None)
            assert batch.profile == selected and tuple(item.values for item in batch.vectors) == ((1., 0., 0.), (0., 1., 0.))
    asyncio.run(exercise())
    assert seen == [{"model": selected.model, "input": [item.instructions + "\n" + item.canonical_text for item in inputs], "dimensions": 3, "encoding_format": "float"}]
    assert "source.invalid" not in json.dumps(seen)


@pytest.mark.parametrize("bad", ["case", "text", "profile", "case_profile", "instructions", "template", "schema", "provider", "token_limit", "oversized_query", "oversized_document", "batch"])
def test_ineligible_inputs_fail_before_any_dispatch_and_case_gate_is_preserved(bad):
    calls = []
    selected = profile()
    candidate = query(selected)
    inputs = (candidate,)
    if bad == "case": inputs = (canonical_text(),)
    elif bad == "text": inputs = ("arbitrary raw text",)
    elif bad == "profile": inputs = (query(profile(model="different-model")),)
    elif bad == "case_profile": selected = embedding_profile(dimensions=3)
    elif bad == "instructions": inputs = (candidate.model_copy(update={"instructions": "Wrong role"}),)
    elif bad == "template": inputs = (candidate.model_copy(update={"template_version": 2}),)
    elif bad == "schema": inputs = (candidate.model_copy(update={"schema_version": 2}),)
    elif bad == "provider":
        selected = profile(provider="unsupported")
        inputs = (query(selected),)
    elif bad == "token_limit":
        selected = profile(max_input_tokens=1)
        inputs = (query(selected),)
    elif bad == "oversized_query": inputs = (candidate.model_copy(update={"question": "中" * 8192}),)
    elif bad == "oversized_document":
        document = CanonicalGuidelineDocumentV1.from_content(content(), selected, load_parameter_capability_registry())
        inputs = (document.model_copy(update={"model_view": document.model_view.model_copy(update={"body": "中" * 24000})}),)
    elif bad == "batch": inputs = (candidate,) * 2049

    def handler(request):
        calls.append(request)
        raise AssertionError("ineligible input dispatched")

    async def exercise():
        async with http_client(handler) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
            with pytest.raises(PortConfigurationError):
                await adapter.embed(selected, inputs, None, None)
            historical = OpenAIEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
            for text in (candidate.canonical_text, "query: raw advisory"):
                with pytest.raises(PortConfigurationError):
                    await historical.embed(embedding_profile(dimensions=3), (text,), None, None)
    asyncio.run(exercise())
    assert not calls


@pytest.mark.parametrize("bad", ["model", "count", "reversed", "duplicate", "boolean_index", "dimension", "zero", "nan", "infinity", "boolean", "string"])
def test_shared_response_validation_rejects_every_malformed_output(bad):
    selected = profile()
    body = response_body(selected.model, [[1, 0, 0], [0, 1, 0]])
    if bad == "model": body["model"] = "wrong-model"
    elif bad == "count": body["data"].pop()
    elif bad == "reversed": body["data"].reverse()
    elif bad == "duplicate": body["data"][1]["index"] = 0
    elif bad == "boolean_index": body["data"][0]["index"] = False
    else:
        body["data"][0]["embedding"] = {
            "dimension": [1, 0], "zero": [0, 0, 0], "nan": [float("nan"), 0, 0],
            "infinity": [float("inf"), 0, 0], "boolean": [True, 0, 0], "string": ["1", 0, 0],
        }[bad]

    async def exercise():
        async with http_client(lambda request: httpx.Response(200, content=json.dumps(body, allow_nan=True))) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
            with pytest.raises(InvalidPortResponseError):
                await adapter.embed(selected, (query(selected), query(selected)), None, None)
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["expired", "timeout", "pre_cancel", "cancel", "task_cancel"])
def test_deadline_and_cancellation_bound_dispatch_and_close_inflight_request(mode):
    selected = profile()
    entered, closed = asyncio.Event(), asyncio.Event()
    calls = []

    class Token:
        requested = mode == "pre_cancel"

        def is_requested(self): return self.requested

    async def handler(request):
        calls.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def exercise():
        token = Token()
        async with http_client(handler) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1", timeout_seconds=0.03 if mode == "timeout" else 3)
            deadline = asyncio.get_running_loop().time() - 1 if mode == "expired" else None
            task = asyncio.create_task(adapter.embed(selected, (query(selected),), deadline, token))
            if mode in {"cancel", "task_cancel"}:
                await asyncio.wait_for(entered.wait(), 1)
                if mode == "cancel": token.requested = True
                else: task.cancel()
            error = asyncio.CancelledError if mode == "task_cancel" else (
                EmbeddingCancelledError if mode in {"pre_cancel", "cancel"} else PortTimeoutError)
            with pytest.raises(error):
                await asyncio.wait_for(task, 1)
            assert len(calls) == (0 if mode in {"expired", "pre_cancel"} else 1)
            if calls: assert closed.is_set()
    asyncio.run(exercise())


def test_online_and_worker_deadlines_are_capped_by_role_and_remaining_session_time():
    from unittest.mock import AsyncMock

    async def exercise():
        selected = profile()
        for timeout in (3, 20):
            http = AsyncMock(spec=ProviderHttpClient)
            http.post_json.return_value = response_body(selected.model, [[1, 0, 0]])
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1", timeout_seconds=timeout)
            for remaining in (1, 100):
                now = asyncio.get_running_loop().time()
                await adapter.embed(selected, (query(selected),), now + remaining, None)
                deadline = http.post_json.call_args.kwargs["deadline"]
                assert now < deadline <= now + min(timeout, remaining) + 0.05
    asyncio.run(exercise())


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, "later"])
def test_invalid_deadline_cannot_bypass_the_online_timeout(deadline):
    async def exercise():
        from unittest.mock import AsyncMock
        http = AsyncMock(spec=ProviderHttpClient)
        adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
        with pytest.raises(PortConfigurationError):
            await adapter.embed(profile(), (query(),), deadline, None)
        http.post_json.assert_not_called()
    asyncio.run(exercise())


def test_cancellation_at_response_completion_does_not_deliver_vectors():
    class Token:
        requested = False

        def is_requested(self): return self.requested

    async def exercise():
        token = Token()
        calls = []

        def handler(request):
            calls.append(request)
            token.requested = True
            return httpx.Response(200, json=response_body(profile().model, [[1, 0, 0]]))

        async with http_client(handler) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/v1")
            with pytest.raises(EmbeddingCancelledError):
                await adapter.embed(profile(), (query(),), None, token)
        assert len(calls) == 1
    asyncio.run(exercise())


def test_provider_failure_is_one_dispatch_and_does_not_leak_payload_or_credentials(caplog):
    calls = []
    selected = profile()

    def handler(request):
        calls.append(request)
        return httpx.Response(500, content="private-provider-envelope")

    async def exercise():
        async with http_client(handler) as http:
            adapter = OpenAIGuidelineEmbeddingAdapter(http, api_key="fixture-secret", base_url="https://provider.test/private")
            with pytest.raises(PortHttpError) as error:
                await adapter.embed(selected, (query(selected),), None, None)
            assert not any(value in str(error.value) for value in ("fixture-secret", "private-provider-envelope", query(selected).canonical_text))
    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        asyncio.run(exercise())
    assert len(calls) == 1
    assert not any(value in caplog.text for value in ("fixture-secret", "private-provider-envelope", query(selected).canonical_text))
