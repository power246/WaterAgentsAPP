from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.mode import InferenceMode
from app.domain.memory_rag.canonical_intent_token import CanonicalIntentToken
from app.domain.memory_rag.meteorological_season import MeteorologicalSeason


_DISCHARGE = ParameterKey(code_system="usgs", code="00060")


def canonical_intent_tokens(
    mode: InferenceMode,
    supporting_parameters: tuple[ParameterKey, ...],
) -> tuple[CanonicalIntentToken, ...]:
    tokens = [CanonicalIntentToken.ANALYZE_CURRENT_STATE]
    if mode in {InferenceMode.GAP_FILLING, InferenceMode.MIXED}:
        tokens.append(CanonicalIntentToken.FILL_GAP)
    if mode in {InferenceMode.PREDICTION, InferenceMode.MIXED}:
        tokens.append(CanonicalIntentToken.PREDICT)
    if _DISCHARGE in supporting_parameters:
        tokens.append(CanonicalIntentToken.REQUIRES_DISCHARGE_SUPPORT)
    return tuple(tokens)


def meteorological_season(month: int) -> MeteorologicalSeason:
    if month in {12, 1, 2}:
        return MeteorologicalSeason.DJF
    if month in {3, 4, 5}:
        return MeteorologicalSeason.MAM
    if month in {6, 7, 8}:
        return MeteorologicalSeason.JJA
    return MeteorologicalSeason.SON


def meteorological_season_year(year: int, month: int) -> int:
    return year + 1 if month == 12 else year
