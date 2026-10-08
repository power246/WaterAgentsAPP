from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from app.application.transcript_memory.embedding_contract import (
    EmbeddingContractError,
    validate_canonical_embedding_texts,
    validated_embedding_batch,
)
from app.domain import EmbeddingBatch, EmbeddingProfile
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    EmbeddingCancellationPort,
    EmbeddingCancelledError,
    InvalidPortResponseError,
    PortConfigurationError,
)

PROVIDER = "openai"
_CANCELLATION_POLL_SECONDS = 0.01


class OpenAIEmbeddingAdapter:
    """Strict OpenAI Embeddings adapter with no retry or transcript side effects."""

    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        api_key: str,
        base_url: str,
    ) -> None:
        if not api_key:
            raise PortConfigurationError(
                "OpenAI API key is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        if not base_url:
            raise PortConfigurationError(
                "OpenAI base URL is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        self._http = http
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/embeddings"

    async def embed(
        self,
        profile: EmbeddingProfile,
        texts: tuple[str, ...],
        deadline: float | None,
        cancel_token: EmbeddingCancellationPort | None,
    ) -> EmbeddingBatch:
        if profile.provider.casefold() != PROVIDER:
            raise PortConfigurationError(
                "The selected embedding provider is not configured.",
                provider=profile.provider,
                operation="embed",
            )
        try:
            validate_canonical_embedding_texts(profile, texts)
        except EmbeddingContractError as error:
            raise PortConfigurationError(
                "Embedding input violates the canonical text contract.",
                provider=PROVIDER,
                operation="embed",
            ) from error
        if not texts:
            return EmbeddingBatch(profile=profile, vectors=())
        if cancel_token is not None and cancel_token.is_requested():
            raise self._cancelled()

        request = self._http.post_json(
            self._endpoint,
            {
                "model": profile.model,
                "input": list(texts),
                "dimensions": profile.output_dimensions,
                "encoding_format": "float",
            },
            provider=PROVIDER,
            operation="embed",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            deadline=deadline,
        )
        response = await self._await_with_cancellation(request, cancel_token)
        try:
            vectors = self._response_vectors(response, profile, len(texts))
            return validated_embedding_batch(
                profile,
                vectors,
                expected_count=len(texts),
            )
        except EmbeddingContractError as error:
            raise self._invalid_response() from error

    @staticmethod
    async def _await_with_cancellation(
        request,
        cancel_token: EmbeddingCancellationPort | None,
    ) -> object:
        if cancel_token is None:
            return await request
        task = asyncio.create_task(request)
        try:
            while not task.done():
                if cancel_token.is_requested():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise OpenAIEmbeddingAdapter._cancelled()
                await asyncio.wait(
                    (task,),
                    timeout=_CANCELLATION_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _response_vectors(
        response: object,
        profile: EmbeddingProfile,
        expected_count: int,
    ) -> tuple[Sequence[object], ...]:
        if not isinstance(response, Mapping):
            raise OpenAIEmbeddingAdapter._invalid_response()
        if response.get("object") != "list" or response.get("model") != profile.model:
            raise OpenAIEmbeddingAdapter._invalid_response()
        data = response.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise OpenAIEmbeddingAdapter._invalid_response()

        vectors: list[Sequence[object]] = []
        for expected_index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise OpenAIEmbeddingAdapter._invalid_response()
            if (
                item.get("object") != "embedding"
                or type(item.get("index")) is not int
                or item.get("index") != expected_index
            ):
                raise OpenAIEmbeddingAdapter._invalid_response()
            vector = item.get("embedding")
            if not isinstance(vector, list):
                raise OpenAIEmbeddingAdapter._invalid_response()
            vectors.append(vector)
        return tuple(vectors)

    @staticmethod
    def _invalid_response() -> InvalidPortResponseError:
        return InvalidPortResponseError(
            "Embedding provider returned an invalid response.",
            provider=PROVIDER,
            operation="embed",
        )

    @staticmethod
    def _cancelled() -> EmbeddingCancelledError:
        return EmbeddingCancelledError(
            "Embedding request was cancelled.",
            provider=PROVIDER,
            operation="embed",
        )
