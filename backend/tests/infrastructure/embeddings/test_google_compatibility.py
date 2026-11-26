import asyncio
import json

import httpx
import pytest

from app.infrastructure.adapters import OpenAIEmbeddingAdapter
from app.infrastructure.adapters.openai_guideline_embeddings import OpenAIGuidelineEmbeddingAdapter
from app.ports import InvalidPortResponseError
from tests.guideline_support import profile, query
from tests.infrastructure.embeddings.support import canonical_text, embedding_profile
from tests.infrastructure.embeddings.test_openai_embeddings import http_client, response_body

GOOGLE = 'https://generativelanguage.googleapis.com/v1beta/openai'


def inputs(kind, count):
    if kind == 'guideline':
        selected = profile()
        return selected, tuple(query(selected).model_copy(update={'question': f'Synthetic topic {i}'}) for i in range(count))
    return embedding_profile(dimensions=3), tuple(canonical_text(parameter_code=code) for code in ('00300', '00010')[:count])


@pytest.mark.parametrize('kind', ['guideline', 'case'])
@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('indexed', [False, True])
def test_google_single_and_batch_preserve_input_order_for_both_ports(kind, count, indexed):
    selected, texts = inputs(kind, count)
    vectors = [[1, 0, 0], [0, 1, 0]][:count]
    body = response_body(selected.model, vectors)
    if not indexed:
        del body['data'][0]['index']
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == GOOGLE + '/embeddings'
        return httpx.Response(200, json=body)

    async def exercise():
        async with http_client(handler) as http:
            cls = OpenAIGuidelineEmbeddingAdapter if kind == 'guideline' else OpenAIEmbeddingAdapter
            port = cls(http, api_key='fixture-google-key', base_url=GOOGLE)
            batch = await port.embed(selected, texts, None, None)
            assert batch.profile == selected
            assert tuple(v.values for v in batch.vectors) == tuple(tuple(v) for v in vectors)
    asyncio.run(exercise())
    assert len(calls) == 1 and len(calls[0]['input']) == count


@pytest.mark.parametrize('kind', ['guideline', 'case'])
@pytest.mark.parametrize('bad', ['other_host', 'lookalike', 'other_path', 'insecure', 'missing_nonzero', 'all_missing', 'null', 'boolean', 'reordered',
    'duplicate', 'count', 'model', 'dimension', 'zero', 'nonfinite'])
def test_google_compatibility_keeps_origin_index_and_vector_validation(kind, bad):
    selected, texts = inputs(kind, 2)
    body = response_body(selected.model, [[1, 0, 0], [0, 1, 0]])
    base_url = GOOGLE
    if bad not in {'boolean', 'reordered', 'duplicate'}:
        del body['data'][0]['index']
    if bad == 'other_host': base_url = 'https://provider.test/v1'
    elif bad == 'lookalike': base_url = 'https://generativelanguage.googleapis.com.provider.test/v1beta/openai'
    elif bad == 'other_path': base_url = 'https://generativelanguage.googleapis.com/v1'
    elif bad == 'insecure': base_url = GOOGLE.replace('https:', 'http:')
    elif bad == 'missing_nonzero':
        body['data'][0]['index'] = 0
        del body['data'][1]['index']
    elif bad == 'all_missing': del body['data'][1]['index']
    elif bad == 'null': body['data'][0]['index'] = None
    elif bad == 'boolean': body['data'][0]['index'] = False
    elif bad == 'reordered': body['data'].reverse()
    elif bad == 'duplicate': body['data'][1]['index'] = 0
    elif bad == 'count': body['data'].pop()
    elif bad == 'model': body['model'] = 'different-model'
    elif bad == 'dimension': body['data'][0]['embedding'] = [1, 0]
    elif bad == 'zero': body['data'][0]['embedding'] = [0, 0, 0]
    elif bad == 'nonfinite': body['data'][0]['embedding'] = [float('nan'), 0, 0]

    async def exercise():
        async with http_client(lambda request: httpx.Response(200, content=json.dumps(body, allow_nan=True))) as http:
            cls = OpenAIGuidelineEmbeddingAdapter if kind == 'guideline' else OpenAIEmbeddingAdapter
            port = cls(http, api_key='fixture-google-key', base_url=base_url)
            with pytest.raises(InvalidPortResponseError):
                await port.embed(selected, texts, None, None)
    asyncio.run(exercise())
