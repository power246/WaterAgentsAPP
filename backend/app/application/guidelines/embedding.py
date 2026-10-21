import asyncio

from app.application.embedding_validation import EmbeddingContractError, MAX_EMBEDDING_BATCH_ITEMS
from app.domain.guidelines import (
    CanonicalGuidelineDocumentV1, CanonicalGuidelineDocumentV2, CanonicalGuidelineQueryV1, GuidelineEmbeddingProfileV1,
)
from app.domain.guidelines.policy import GuidelinePolicy
from app.domain.guidelines.canonical_document import MAX_GUIDELINE_DOCUMENT_BYTES
from app.domain.guidelines.canonical_query import MAX_GUIDELINE_QUERY_BYTES
from app.domain.model_catalog_snapshot import ModelCatalogSnapshot
from app.ports import (
    EmbeddingCancelledError, InvalidPortResponseError, PortAuthenticationError, PortConfigurationError,
    PortHttpError, PortRateLimitError, PortResponseLimitError, PortTimeoutError, PortUnavailableError,
)


def desired_guideline_profile(catalog: ModelCatalogSnapshot, policy: GuidelinePolicy) -> GuidelineEmbeddingProfileV1:
    try:
        selected = catalog.embedding_profile
        return GuidelineEmbeddingProfileV1(
            provider=selected.provider, model=selected.model, output_dimensions=selected.output_dimensions,
            max_input_tokens=selected.max_input_tokens, document_instructions=policy.document_instructions,
            query_instructions=policy.query_instructions,
        )
    except (StopIteration, ValueError):
        raise EmbeddingContractError("guideline embedding configuration is unavailable") from None


def validated_guideline_texts(profile: GuidelineEmbeddingProfileV1, inputs) -> tuple[str, ...]:
    try:
        if not isinstance(profile, GuidelineEmbeddingProfileV1):
            raise ValueError()
        profile = GuidelineEmbeddingProfileV1.model_validate(profile.model_dump())
        if not isinstance(inputs, tuple) or len(inputs) > MAX_EMBEDDING_BATCH_ITEMS:
            raise ValueError()
        texts = []
        for item in inputs:
            if type(item) not in {CanonicalGuidelineDocumentV1, CanonicalGuidelineDocumentV2, CanonicalGuidelineQueryV1}:
                raise ValueError()
            checked = type(item).model_validate_json(item.canonical_text)
            checked.require_profile(profile)
            if checked.canonical_text != item.canonical_text:
                raise ValueError()
            text = checked.instructions + "\n" + checked.canonical_text
            # A conservative UTF-8 byte upper bound avoids a model-specific tokenizer.
            byte_limit = (MAX_GUIDELINE_DOCUMENT_BYTES if isinstance(checked, (CanonicalGuidelineDocumentV1, CanonicalGuidelineDocumentV2))
                          else MAX_GUIDELINE_QUERY_BYTES)
            if len(text.encode("utf-8")) > min(byte_limit, profile.max_input_tokens):
                raise ValueError()
            texts.append(text)
        return tuple(texts)
    except ValueError:
        raise EmbeddingContractError("guideline embedding input violates its profile or text limits") from None


def guideline_embedding_failure(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, (EmbeddingCancelledError, asyncio.CancelledError)):
        return "guideline_embedding_cancelled", False
    if isinstance(error, (PortTimeoutError, TimeoutError)):
        return "guideline_embedding_timeout", True
    if isinstance(error, PortAuthenticationError):
        return "guideline_embedding_credentials_unavailable", False
    if isinstance(error, (EmbeddingContractError, PortConfigurationError)):
        return "guideline_embedding_configuration_invalid", False
    if isinstance(error, (InvalidPortResponseError, PortResponseLimitError)):
        return "guideline_embedding_response_invalid", False
    if isinstance(error, PortRateLimitError):
        return "guideline_embedding_rate_limited", True
    if isinstance(error, PortHttpError):
        return "guideline_embedding_unavailable", error.status_code is not None and error.status_code >= 500
    if isinstance(error, PortUnavailableError):
        return "guideline_embedding_unavailable", True
    return "guideline_embedding_failed", False
