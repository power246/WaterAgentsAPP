from hashlib import sha256
import json

import pytest
from pydantic import ValidationError

from app.domain import EmbeddingProfile
from tests.infrastructure.embeddings.support import embedding_profile


def _identity_digest(values: dict[str, object]) -> str:
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(canonical).hexdigest()}"


def test_every_profile_field_participates_in_the_stable_identity() -> None:
    profile = embedding_profile()
    identity = profile.model_dump(
        mode="json",
        exclude={"profile_id", "profile_digest"},
    )
    assert profile.profile_id == profile.profile_digest == _identity_digest(identity)

    mutations = {
        "provider": "other-provider",
        "model": "other-model",
        "output_dimensions": 9,
        "max_input_tokens": 513,
        "distance_metric": "euclidean",
        "semantic_min_similarity": 0.76,
        "canonical_query_schema_version": 2,
        "canonical_template_version": 2,
        "projection_schema_version": 2,
    }
    for field_name, value in mutations.items():
        changed = {**identity, field_name: value}
        assert _identity_digest(changed) != profile.profile_id

    accepted = (
        embedding_profile(provider="other-provider"),
        embedding_profile(model="other-model"),
        embedding_profile(dimensions=9),
        embedding_profile(max_input_tokens=513),
        embedding_profile(semantic_min_similarity=0.76),
    )
    assert all(item.profile_id != profile.profile_id for item in accepted)


@pytest.mark.parametrize("field_name", ("api_key", "credential", "endpoint"))
def test_secret_or_transport_values_cannot_enter_profile_identity(
    field_name: str,
) -> None:
    values = embedding_profile().model_dump(mode="python")
    values[field_name] = "must-not-be-persisted"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EmbeddingProfile.model_validate(values)
