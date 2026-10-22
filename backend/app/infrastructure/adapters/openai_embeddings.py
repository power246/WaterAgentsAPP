from app.application.transcript_memory.embedding_contract import (
    EmbeddingContractError, validate_canonical_embedding_texts,
)
from app.domain import EmbeddingBatch, EmbeddingProfile
from app.infrastructure.adapters.openai_embedding_transport import OpenAIEmbeddingTransport, PROVIDER
from app.infrastructure.http import ProviderHttpClient
from app.ports import EmbeddingCancellationPort, PortConfigurationError


class OpenAIEmbeddingAdapter:
    """Historical case inputs retain their canonical eligibility gate."""

    def __init__(self, http: ProviderHttpClient, *, api_key: str, base_url: str) -> None:
        self._transport = OpenAIEmbeddingTransport(http, api_key=api_key, base_url=base_url)

    async def embed(self, profile: EmbeddingProfile, texts: tuple[str, ...], deadline: float | None,
                    cancel_token: EmbeddingCancellationPort | None) -> EmbeddingBatch:
        if profile.provider.casefold() != PROVIDER:
            raise PortConfigurationError(
                "The selected embedding provider is not configured.", provider=profile.provider, operation="embed")
        try:
            validate_canonical_embedding_texts(profile, texts)
        except EmbeddingContractError:
            raise PortConfigurationError(
                "Embedding input violates the canonical text contract.", provider=PROVIDER, operation="embed") from None
        vectors = await self._transport.embed(
            model=profile.model, dimensions=profile.output_dimensions,
            texts=texts, deadline=deadline, cancel_token=cancel_token)
        return EmbeddingBatch(profile=profile, vectors=vectors)
