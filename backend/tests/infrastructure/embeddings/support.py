from app.domain import (
    CanonicalCaseQueryV1,
    CanonicalIntentToken,
    CanonicalWindowDescriptorV1,
    EmbeddingProfile,
    InferenceMode,
    MeteorologicalSeason,
    ParameterKey,
    TimeResolution,
)


def embedding_profile(
    *,
    provider: str = "openai",
    model: str = "text-embedding-fixture",
    dimensions: int = 8,
    max_input_tokens: int = 512,
    semantic_min_similarity: float = 0.75,
) -> EmbeddingProfile:
    return EmbeddingProfile(
        provider=provider,
        model=model,
        output_dimensions=dimensions,
        max_input_tokens=max_input_tokens,
        semantic_min_similarity=semantic_min_similarity,
    )


def canonical_text(
    *,
    mode: InferenceMode = InferenceMode.PREDICTION,
    parameter_code: str = "00300",
    supporting_code: str = "00060",
    duration_days: str = "7",
    month: int = 7,
    season: MeteorologicalSeason = MeteorologicalSeason.JJA,
    resolution: TimeResolution = TimeResolution.TWENTY_FOUR_HOURS,
) -> str:
    return CanonicalCaseQueryV1(
        mode=mode,
        requested_parameter_keys=(
            ParameterKey(code_system="usgs", code=parameter_code),
        ),
        supporting_parameter_keys=(
            ParameterKey(code_system="usgs", code=supporting_code),
        ),
        window_descriptors=(
            CanonicalWindowDescriptorV1(
                window_kind=mode,
                duration_days=duration_days,
                local_months=(month,),
                season_buckets=(season,),
                resolution=resolution,
            ),
        ),
        normalized_intent_tokens=(
            CanonicalIntentToken.ANALYZE_CURRENT_STATE,
            CanonicalIntentToken.PREDICT,
            CanonicalIntentToken.REQUIRES_DISCHARGE_SUPPORT,
        ),
    ).canonical_text
