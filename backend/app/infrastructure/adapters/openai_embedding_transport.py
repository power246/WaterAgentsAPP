import asyncio
from collections.abc import Mapping

from app.application.embedding_validation import EmbeddingContractError, validated_embedding_vectors
from app.infrastructure.http import ProviderHttpClient
from app.ports import EmbeddingCancellationPort, EmbeddingCancelledError, InvalidPortResponseError, PortConfigurationError

PROVIDER = "openai"


class OpenAIEmbeddingTransport:
    """One bounded request and shared output validation, without input semantics."""

    def __init__(self, http: ProviderHttpClient, *, api_key: str, base_url: str) -> None:
        if not api_key or not base_url:
            raise PortConfigurationError("Embedding provider is not configured.", provider=PROVIDER, operation="configure")
        self._http = http
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/embeddings"
        self._omit_zero_index = base_url.rstrip('/') == 'https://generativelanguage.googleapis.com/v1beta/openai'

    async def embed(self, *, model: str, dimensions: int, texts: tuple[str, ...],
                    deadline: float | None, cancel_token: EmbeddingCancellationPort | None):
        if not texts:
            return ()
        if cancel_token is not None and cancel_token.is_requested():
            raise self._cancelled()
        request = self._http.post_json(
            self._endpoint,
            {"model": model, "input": list(texts), "dimensions": dimensions, "encoding_format": "float"},
            provider=PROVIDER, operation="embed",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            deadline=deadline,
        )
        response = await self._await_with_cancellation(request, cancel_token)
        if cancel_token is not None and cancel_token.is_requested():
            raise self._cancelled()
        try:
            vectors = self._response_vectors(response, model, len(texts), omit_zero_index=self._omit_zero_index)
            return validated_embedding_vectors(vectors, dimensions=dimensions, expected_count=len(texts))
        except EmbeddingContractError:
            raise self._invalid_response() from None

    @staticmethod
    async def _await_with_cancellation(request, cancel_token):
        if cancel_token is None:
            return await request
        task = asyncio.create_task(request)
        try:
            while not task.done():
                if cancel_token.is_requested():
                    raise OpenAIEmbeddingTransport._cancelled()
                await asyncio.wait((task,), timeout=0.01, return_when=asyncio.FIRST_COMPLETED)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _response_vectors(response, model, expected_count, *, omit_zero_index=False):
        if not isinstance(response, Mapping) or response.get("object") != "list" or response.get("model") != model:
            raise OpenAIEmbeddingTransport._invalid_response()
        data = response.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise OpenAIEmbeddingTransport._invalid_response()
        vectors = []
        for index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise OpenAIEmbeddingTransport._invalid_response()
            # Google's compatibility endpoint omits the default index value of zero.
            item_index = item.get('index', 0 if omit_zero_index and index == 0 else None)
            if (item.get("object") != "embedding"
                    or type(item_index) is not int or item_index != index
                    or not isinstance(item.get("embedding"), list)):
                raise OpenAIEmbeddingTransport._invalid_response()
            vectors.append(item["embedding"])
        return tuple(vectors)

    @staticmethod
    def _invalid_response():
        return InvalidPortResponseError("Embedding provider returned an invalid response.", provider=PROVIDER, operation="embed")

    @staticmethod
    def _cancelled():
        return EmbeddingCancelledError("Embedding request was cancelled.", provider=PROVIDER, operation="embed")
