import pytest
from pydantic import ValidationError

from app.domain.model_catalog import (
    ModelCapability,
    ModelCatalogSnapshot,
    ModelRole,
    ModelSelection,
)


def test_catalog_requires_unique_safe_identities_and_explicit_roles() -> None:
    capability = ModelCapability(
        provider="provider",
        model="model",
        roles=(ModelRole.QUERY_PARSER,),
    )
    assert ModelCatalogSnapshot(capabilities=(capability,)).supports(
        ModelSelection(provider="provider", model="model"),
        ModelRole.QUERY_PARSER,
    )
    assert not ModelCatalogSnapshot(capabilities=(capability,)).supports(
        ModelSelection(provider="provider", model="model"),
        ModelRole.WATERBODY,
    )

    with pytest.raises(ValidationError, match="duplicates"):
        ModelCatalogSnapshot(capabilities=(capability, capability))
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
