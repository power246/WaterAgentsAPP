import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from app.domain import InferenceMode, MeteorologicalSeason, TimeResolution
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.infrastructure.embeddings.support import canonical_text, embedding_profile


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        math.sqrt(sum(value * value for value in left))
        * math.sqrt(sum(value * value for value in right))
    )


def test_fake_is_stable_across_instances_and_supports_similarity_fixtures() -> None:
    async def exercise():
        profile = embedding_profile(dimensions=32)
        original = canonical_text()
        similar = canonical_text(parameter_code="00301")
        dissimilar = canonical_text(
            mode=InferenceMode.GAP_FILLING,
            parameter_code="00940",
            supporting_code="63680",
            duration_days="30",
            month=1,
            season=MeteorologicalSeason.DJF,
            resolution=TimeResolution.SIX_HOURS,
        )
        first = await DeterministicEmbeddingPort().embed(
            profile,
            (original, similar, dissimilar),
            None,
            None,
        )
        replay = await DeterministicEmbeddingPort().embed(
            profile,
            (original,),
            None,
            None,
        )
        return first, replay

    first, replay = asyncio.run(exercise())
    original, similar, dissimilar = (
        vector.values for vector in first.vectors
    )
    assert original == replay.vectors[0].values
    assert _cosine(original, similar) > _cosine(original, dissimilar)
    assert _cosine(original, similar) > 0.5


def test_fake_vector_has_a_cross_process_golden_without_python_hash() -> None:
    script = """
import asyncio, json
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.infrastructure.embeddings.support import canonical_text, embedding_profile
async def main():
    batch = await DeterministicEmbeddingPort().embed(embedding_profile(dimensions=8), (canonical_text(),), None, None)
    print(json.dumps(batch.vectors[0].values, separators=(',', ':')))
asyncio.run(main())
"""
    backend_root = Path(__file__).parents[3]
    outputs = []
    for seed in ("1", "8675309"):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=backend_root,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout.strip())

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == [
        -0.29488391230979427,
        -0.14744195615489714,
        -0.29488391230979427,
        0.0,
        -0.5897678246195885,
        0.29488391230979427,
        -0.5897678246195885,
        -0.14744195615489714,
    ]
