import pytest

from app.application.transcript_memory.case_policy import (
    canonical_intent_tokens,
    meteorological_season,
    meteorological_season_year,
)
from app.domain import (
    CanonicalIntentToken,
    InferenceMode,
    MeteorologicalSeason,
    ParameterKey,
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (
            InferenceMode.GAP_FILLING,
            (CanonicalIntentToken.ANALYZE_CURRENT_STATE, CanonicalIntentToken.FILL_GAP),
        ),
        (
            InferenceMode.PREDICTION,
            (CanonicalIntentToken.ANALYZE_CURRENT_STATE, CanonicalIntentToken.PREDICT),
        ),
        (
            InferenceMode.MIXED,
            (
                CanonicalIntentToken.ANALYZE_CURRENT_STATE,
                CanonicalIntentToken.FILL_GAP,
                CanonicalIntentToken.PREDICT,
            ),
        ),
    ),
)
def test_case_intent_tokens_are_stable_for_index_and_search(mode, expected) -> None:
    discharge = ParameterKey(code_system="usgs", code="00060")
    assert canonical_intent_tokens(mode, ()) == expected
    assert canonical_intent_tokens(mode, (discharge,)) == (
        *expected,
        CanonicalIntentToken.REQUIRES_DISCHARGE_SUPPORT,
    )


@pytest.mark.parametrize(
    ("month", "season", "season_year"),
    (
        (1, MeteorologicalSeason.DJF, 2026),
        (3, MeteorologicalSeason.MAM, 2026),
        (6, MeteorologicalSeason.JJA, 2026),
        (9, MeteorologicalSeason.SON, 2026),
        (12, MeteorologicalSeason.DJF, 2027),
    ),
)
def test_case_season_and_year_follow_the_same_calendar_policy(
    month, season, season_year
) -> None:
    assert meteorological_season(month) is season
    assert meteorological_season_year(2026, month) == season_year
