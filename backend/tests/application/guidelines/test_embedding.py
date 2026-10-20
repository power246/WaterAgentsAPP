import asyncio

import pytest
from pydantic import ValidationError

from app.application.embedding_validation import EmbeddingContractError, validated_embedding_vectors
from app.application.guidelines.embedding import (
    desired_guideline_profile, guideline_embedding_failure, validated_guideline_texts,
)
from app.domain import ModelCapability, ModelCatalogSnapshot, ModelRole
from app.domain.guidelines import CanonicalGuidelineDocumentV1, GuidelinePolicy
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.ports import (
    EmbeddingCancelledError, InvalidPortResponseError, PortAuthenticationError, PortConfigurationError,
    PortHttpError, PortRateLimitError, PortResponseLimitError, PortTimeoutError, PortUnavailableError,
)
from tests.guideline_support import content, profile, query
from tests.infrastructure.embeddings.support import embedding_profile


def catalog(**overrides):
    selected = embedding_profile(dimensions=3).model_dump(exclude={"profile_id", "profile_digest"})
    from app.domain import EmbeddingProfile
    selected = EmbeddingProfile.model_validate({**selected, **overrides})
    return ModelCatalogSnapshot(capabilities=(ModelCapability(
        provider=selected.provider, model=selected.model, roles=(ModelRole.EMBEDDING,), embedding_profile=selected,
    ),))


def test_profile_uses_catalog_configuration_and_separates_case_identity_and_threshold():
    configured = catalog(max_input_tokens=10_000, semantic_min_similarity=0.99)
    policy = GuidelinePolicy(document_instructions="passage: ", query_instructions="query: ")
    selected = desired_guideline_profile(configured, policy)
    case = configured.embedding_profile
    assert (selected.provider, selected.model, selected.output_dimensions, selected.max_input_tokens) == (
        case.provider, case.model, case.output_dimensions, case.max_input_tokens,
    )
    assert selected.profile_id != case.profile_id and selected.profile_id.startswith("guideline:")
    assert policy.semantic_min_similarity == 0.35
    changed = desired_guideline_profile(configured, GuidelinePolicy(query_instructions="Find related mechanisms."))
    assert changed.profile_id != selected.profile_id
    assert desired_guideline_profile(catalog(max_input_tokens=9999), policy).profile_id != selected.profile_id
    assert desired_guideline_profile(catalog(semantic_min_similarity=0.01, max_input_tokens=10_000), policy) == selected
    with pytest.raises(EmbeddingContractError):
        desired_guideline_profile(ModelCatalogSnapshot(capabilities=()), policy)


def test_policy_defaults_and_deployment_override_have_one_validated_owner(monkeypatch):
    policy = GuidelinePolicy()
    assert (policy.top_k, policy.semantic_min_similarity, policy.knowledge_budget_tokens, policy.max_query_bytes) == (3, 0.35, 3072, 8192)
    assert (policy.online_timeout_seconds, policy.online_attempts) == (3, 1)
    assert (policy.worker_max_concurrency, policy.worker_timeout_seconds, policy.max_attempts, policy.retry_backoff_seconds) == (2, 20, 3, (2, 10))
    monkeypatch.setenv("WATERAGENTS_GUIDELINE_POLICY", '{"query_instructions":"query: ","worker_max_concurrency":1}')
    settings = AppSettings(_env_file=None)
    assert settings.guideline_policy.query_instructions == "query: "
    assert settings.guideline_policy.worker_max_concurrency == 1
    assert settings.guideline_policy.document_instructions == policy.document_instructions


@pytest.mark.parametrize("change", [
    {"top_k": True}, {"top_k": 4}, {"semantic_min_similarity": float("nan")}, {"semantic_min_similarity": -0.1},
    {"knowledge_budget_tokens": 0}, {"max_query_bytes": 8193}, {"online_timeout_seconds": 4},
    {"online_timeout_seconds": True}, {"online_attempts": 2}, {"online_attempts": True},
    {"worker_max_concurrency": 0}, {"worker_timeout_seconds": float("inf")}, {"max_attempts": 4},
    {"retry_backoff_seconds": (1, 2)}, {"retry_backoff_seconds": (2.0, 10)},
    {"query_instructions": "   "}, {"document_instructions": "api_key=private"},
])
def test_policy_rejects_unsafe_or_unbounded_configuration(change):
    with pytest.raises(ValidationError):
        GuidelinePolicy(**change)


def test_canonical_roles_are_prefixed_stable_and_limit_complete_input_without_truncation():
    selected = profile(max_input_tokens=10_000, document_instructions="passage: ", query_instructions="query: ")
    document = CanonicalGuidelineDocumentV1.from_content(content(), selected, load_parameter_capability_registry())
    question = query(selected)
    texts = validated_guideline_texts(selected, (document, question))
    assert texts == ("passage: \n" + document.canonical_text, "query: \n" + question.canonical_text)
    assert content().body_markdown in texts[0]
    other = CanonicalGuidelineDocumentV1.from_content(content(body_markdown="中文条件\r\nOtherwise retain C."), selected, load_parameter_capability_registry())
    equivalent = CanonicalGuidelineDocumentV1.from_content(content(body_markdown="中文条件\nOtherwise retain C."), selected, load_parameter_capability_registry())
    assert validated_guideline_texts(selected, (other,)) == validated_guideline_texts(selected, (equivalent,))
    tiny = profile(max_input_tokens=1)
    with pytest.raises(EmbeddingContractError):
        validated_guideline_texts(tiny, (query(tiny),))
    assert question.question == "解释上下游酸碱变化"
    size = len(validated_guideline_texts(profile(), (query(),))[0].encode("utf-8"))
    boundary = profile(max_input_tokens=size)
    assert len(validated_guideline_texts(boundary, (query(boundary),))[0].encode("utf-8")) == size
    exceeded = profile(max_input_tokens=size - 1)
    with pytest.raises(EmbeddingContractError):
        validated_guideline_texts(exceeded, (query(exceeded),))


@pytest.mark.parametrize("kind", ["query", "document"])
def test_role_prefix_counts_toward_the_complete_wire_input_byte_limit(kind):
    selected = profile(max_input_tokens=100_000)
    prefix_size = len((selected.query_instructions + "\n" if kind == "query"
                       else selected.document_instructions + "\n").encode("utf-8"))
    limit = 8192 if kind == "query" else 24_000
    if kind == "query":
        def make(extra): return query(selected, question="q" * extra)
        empty = query(selected, question="")
    else:
        document = CanonicalGuidelineDocumentV1.from_content(content(), selected, load_parameter_capability_registry())
        def make(extra): return CanonicalGuidelineDocumentV1.model_validate({**document.model_dump(), "parameter_names": ("p" * (extra + 1),)})
        empty = make(0)
    size = len(empty.canonical_text.encode("utf-8"))
    boundary = make(limit - size - prefix_size)
    assert len(validated_guideline_texts(selected, (boundary,))[0].encode("utf-8")) == limit
    overflow = make(limit - size - prefix_size + 1)
    assert len(overflow.canonical_text.encode("utf-8")) <= limit
    with pytest.raises(EmbeddingContractError):
        validated_guideline_texts(selected, (overflow,))


@pytest.mark.parametrize(("error", "code", "retryable"), [
    (EmbeddingCancelledError("private"), "guideline_embedding_cancelled", False),
    (asyncio.CancelledError(), "guideline_embedding_cancelled", False),
    (PortTimeoutError("private"), "guideline_embedding_timeout", True),
    (TimeoutError("private"), "guideline_embedding_timeout", True),
    (PortAuthenticationError("private"), "guideline_embedding_credentials_unavailable", False),
    (PortConfigurationError("private"), "guideline_embedding_configuration_invalid", False),
    (EmbeddingContractError("private"), "guideline_embedding_configuration_invalid", False),
    (InvalidPortResponseError("private"), "guideline_embedding_response_invalid", False),
    (PortResponseLimitError("private"), "guideline_embedding_response_invalid", False),
    (PortRateLimitError("private"), "guideline_embedding_rate_limited", True),
    (PortUnavailableError("private"), "guideline_embedding_unavailable", True),
    (PortHttpError("private", status_code=503), "guideline_embedding_unavailable", True),
    (PortHttpError("private", status_code=400), "guideline_embedding_unavailable", False),
    (RuntimeError("private"), "guideline_embedding_failed", False),
])
def test_safe_failure_taxonomy_excludes_provider_diagnostics(error, code, retryable):
    assert guideline_embedding_failure(error) == (code, retryable)


@pytest.mark.parametrize("values", [[True, 0, 0], ["1", 0, 0], [10**1000, 0, 0], [1e308, 0, 0], [1e-20, 0, 0]])
def test_shared_validation_rejects_non_numeric_and_extreme_vectors(values):
    with pytest.raises(EmbeddingContractError):
        validated_embedding_vectors((values,), dimensions=3, expected_count=1)
