import asyncio
import inspect
from typing import get_type_hints

import pytest

from app.domain.guidelines import (
    CanonicalGuidelineDocumentV1, GuidelineEmbeddingBatch, GuidelineEmbeddingProfileV1,
    GuidelineMutationReceipt, GuidelineOperation, GuidelineVersionV1,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import BundledGuidelineSourcePort, GuidelineEmbeddingPort, GuidelineRepositoryPort
from app.ports.errors import EmbeddingCancelledError
from tests.guideline_support import content, profile, query, version


def test_guideline_ports_are_public_protocols_with_domain_envelopes():
    for protocol in (BundledGuidelineSourcePort, GuidelineEmbeddingPort, GuidelineRepositoryPort):
        assert protocol._is_protocol
    assert get_type_hints(GuidelineEmbeddingPort.embed)["profile"] is GuidelineEmbeddingProfileV1
    assert get_type_hints(GuidelineEmbeddingPort.embed)["return"] is GuidelineEmbeddingBatch
    assert get_type_hints(GuidelineRepositoryPort.publish_version)["version"] is GuidelineVersionV1
    assert get_type_hints(GuidelineRepositoryPort.publish_version)["return"] is GuidelineMutationReceipt
    assert get_type_hints(GuidelineRepositoryPort.read_operation)["return"] == GuidelineOperation | None
    assert get_type_hints(BundledGuidelineSourcePort.read_versions)["return"] == tuple[GuidelineVersionV1, ...]
    assert not inspect.iscoroutinefunction(BundledGuidelineSourcePort.read_versions)
    assert all(inspect.iscoroutinefunction(method) for name, method in inspect.getmembers(
        GuidelineRepositoryPort, inspect.isfunction,
    ) if not name.startswith("_"))


def test_typed_embedding_port_supports_both_roles_and_shared_cancellation():
    class Cancelled:
        def is_requested(self):
            return True

    class FixtureEmbedding:
        def __init__(self):
            self.inputs = ()

        async def embed(self, selected, inputs, deadline, cancel_token):
            if cancel_token is not None and cancel_token.is_requested():
                raise EmbeddingCancelledError("fixture cancelled")
            for value in inputs:
                value.require_profile(selected)
            self.inputs = inputs
            return GuidelineEmbeddingBatch(
                profile=selected, vectors=tuple({"values": (1.0, 0.0, 0.0)} for _ in inputs),
            )

    async def exercise():
        selected = profile()
        document = CanonicalGuidelineDocumentV1.from_content(content(), selected, load_parameter_capability_registry())
        implementation = FixtureEmbedding()
        port: GuidelineEmbeddingPort = implementation
        with pytest.raises(EmbeddingCancelledError):
            await port.embed(selected, (document,), 20.0, Cancelled())
        assert implementation.inputs == ()
        result = await port.embed(selected, (document, query(selected)), 20.0, None)
        assert len(result.vectors) == 2
        assert result.profile == selected
        with pytest.raises(ValueError, match="profile"):
            await port.embed(profile(model="different-model"), (document,), 20.0, None)

    asyncio.run(exercise())


def test_bundle_source_returns_explicit_reviewed_versions():
    fixture_version = version()

    class FixtureSource:
        def read_versions(self):
            return (fixture_version,)

    source: BundledGuidelineSourcePort = FixtureSource()
    assert source.read_versions()[0].review.review_note.startswith("Synthetic fixture approval")
