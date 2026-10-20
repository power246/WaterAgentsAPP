import asyncio
from math import isfinite

from app.application.embedding_validation import EmbeddingContractError
from app.application.guidelines.embedding import validated_guideline_texts
from app.domain.guidelines import (
    CanonicalGuidelineDocumentV1, CanonicalGuidelineQueryV1, GuidelineEmbeddingBatch, GuidelineEmbeddingProfileV1,
)
from app.domain.guidelines.policy import DEFAULT_GUIDELINE_POLICY
from app.infrastructure.adapters.openai_embedding_transport import OpenAIEmbeddingTransport, PROVIDER
from app.infrastructure.http import ProviderHttpClient
from app.ports import EmbeddingCancellationPort, PortConfigurationError


class OpenAIGuidelineEmbeddingAdapter:
    def __init__(self, http: ProviderHttpClient, *, api_key: str, base_url: str,
                 timeout_seconds: float = DEFAULT_GUIDELINE_POLICY.online_timeout_seconds) -> None:
        if isinstance(timeout_seconds, bool) or not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("guideline embedding timeout must be finite and positive")
        self._timeout = timeout_seconds
        self._transport = OpenAIEmbeddingTransport(http, api_key=api_key, base_url=base_url)

    async def embed(self, profile: GuidelineEmbeddingProfileV1,
                    inputs: tuple[CanonicalGuidelineDocumentV1 | CanonicalGuidelineQueryV1, ...],
                    deadline: float | None, cancel_token: EmbeddingCancellationPort | None) -> GuidelineEmbeddingBatch:
        try:
            texts = validated_guideline_texts(profile, inputs)
            if profile.provider.casefold() != PROVIDER:
                raise EmbeddingContractError("unsupported guideline embedding provider")
            if deadline is not None and (type(deadline) not in {int, float} or not isfinite(deadline)):
                raise EmbeddingContractError("invalid guideline embedding deadline")
        except EmbeddingContractError:
            raise PortConfigurationError("Guideline embedding input or profile is invalid.",
                                         provider=PROVIDER, operation="embed") from None
        bounded_deadline = asyncio.get_running_loop().time() + self._timeout
        if deadline is not None:
            bounded_deadline = min(deadline, bounded_deadline)
        vectors = await self._transport.embed(model=profile.model, dimensions=profile.output_dimensions,
            texts=texts, deadline=bounded_deadline, cancel_token=cancel_token)
        return GuidelineEmbeddingBatch(profile=profile, vectors=vectors)
