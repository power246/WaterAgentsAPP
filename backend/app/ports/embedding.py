from typing import Protocol

from app.domain import EmbeddingBatch, EmbeddingProfile, NonEmptyString


class EmbeddingCancellationPort(Protocol):
    def is_requested(self) -> bool: ...


class EmbeddingPort(Protocol):
    async def embed(
        self,
        profile: EmbeddingProfile,
        texts: tuple[NonEmptyString, ...],
        deadline: float | None,
        cancel_token: EmbeddingCancellationPort | None,
    ) -> EmbeddingBatch: ...
