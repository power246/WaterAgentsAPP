import pytest
from pydantic import ValidationError

from app.domain.model_catalog import (
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
)
from app.domain import EmbeddingProfile


def embedding_capability(model: str = "embedding-v1") -> ModelCapability:
    profile = EmbeddingProfile(
        provider="provider",
        model=model,
        output_dimensions=8,
        max_input_tokens=512,
        semantic_min_similarity=0.78,
    )
    return ModelCapability(
        provider=profile.provider,
        model=profile.model,
        roles=(ModelRole.EMBEDDING,),
        embedding_profile=profile,
    )


def test_catalog_requires_unique_safe_identities_and_explicit_roles() -> None:
    capability = ModelCapability(
        provider="provider",
        model="model",
        roles=(ModelRole.QUERY_PARSER,),
    )
    catalog = ModelCatalogSnapshot(
        capabilities=(capability, embedding_capability())
    )
    assert catalog.supports(
        ModelSelection(provider="provider", model="model"),
        ModelRole.QUERY_PARSER,
    )
    assert not catalog.supports(
        ModelSelection(provider="provider", model="model"),
        ModelRole.WATERBODY,
    )

    with pytest.raises(ValidationError, match="duplicates"):
        ModelCatalogSnapshot(
            capabilities=(capability, capability, embedding_capability())
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        ModelCapability(provider="provider", model="model", roles=())


@pytest.mark.parametrize("secret_field", ["api_key", "credential", "endpoint"])
def test_model_capability_rejects_secret_or_transport_fields(secret_field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelCapability.model_validate(
            {
                "provider": "provider",
                "model": "model",
                "roles": ["query_parser"],
                secret_field: "must-not-be-stored",
            }
        )


def test_waterbody_capability_requires_model_specific_limits() -> None:
    capability = ModelCapability(
        provider="provider",
        model="waterbody-large",
        roles=(ModelRole.WATERBODY,),
        context_window_tokens=128_000,
        max_output_tokens=32_768,
    )

    assert capability.context_window_tokens == 128_000
    assert capability.max_output_tokens == 32_768

    for values in (
        {},
        {"context_window_tokens": 128_000},
        {"max_output_tokens": 32_768},
        {"context_window_tokens": 1_000, "max_output_tokens": 1_001},
    ):
        with pytest.raises(ValidationError):
            ModelCapability(
                provider="provider",
                model="waterbody-invalid",
                roles=(ModelRole.WATERBODY,),
                **values,
            )

    with pytest.raises(ValidationError, match="only valid for waterbody"):
        ModelCapability(
            provider="provider",
            model="parser-with-irrelevant-limits",
            roles=(ModelRole.QUERY_PARSER,),
            context_window_tokens=8_192,
            max_output_tokens=2_048,
        )
