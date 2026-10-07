from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from math import sqrt

from app.application.transcript_memory.embedding_contract import (
    validate_canonical_embedding_texts,
    validated_embedding_batch,
)
from app.domain import EmbeddingBatch, EmbeddingProfile
from app.ports import (
    EmbeddingCancellationPort,
    EmbeddingCancelledError,
    PortTimeoutError,
)


class DeterministicEmbeddingPort:
    """No-network test port; it is never selected by production composition."""

    def __init__(self) -> None:
        self.call_count = 0

    async def embed(
        self,
        profile: EmbeddingProfile,
        texts: tuple[str, ...],
        deadline: float | None,
        cancel_token: EmbeddingCancellationPort | None,
    ) -> EmbeddingBatch:
        validate_canonical_embedding_texts(profile, texts)
        if cancel_token is not None and cancel_token.is_requested():
            raise EmbeddingCancelledError(
                "Embedding request was cancelled.",
                provider="deterministic-test",
                operation="embed",
            )
        if deadline is not None and deadline <= asyncio.get_running_loop().time():
            raise PortTimeoutError(
                "Embedding request deadline was exhausted.",
                provider="deterministic-test",
                operation="embed",
            )
        self.call_count += 1
        vectors = tuple(self._vector(profile, text) for text in texts)
        return validated_embedding_batch(
            profile,
            vectors,
            expected_count=len(texts),
        )

    @staticmethod
    def _vector(profile: EmbeddingProfile, text: str) -> tuple[float, ...]:
        values = [0.0] * profile.output_dimensions
        payload = json.loads(text)
        tokens = sorted(set(_leaf_tokens(payload)))
        seed = profile.profile_digest.encode("utf-8")
        for token in tokens:
            digest = sha256(seed + b"\0" + token.encode("utf-8")).digest()
            for offset in (0, 8, 16, 24):
                index = int.from_bytes(digest[offset : offset + 4], "big") % len(values)
                sign = 1.0 if digest[offset + 4] & 1 else -1.0
                values[index] += sign
        if not any(values):
            values[0] = 1.0
        norm = sqrt(sum(value * value for value in values))
        return tuple(value / norm for value in values)


def _leaf_tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        return tuple(
            token
            for key in sorted(value)
            for token in _leaf_tokens(value[key])
        )
    if isinstance(value, list):
        return tuple(token for item in value for token in _leaf_tokens(item))
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("true" if value else "false",)
    return (str(value),)
