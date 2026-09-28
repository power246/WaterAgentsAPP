import asyncio

import pytest

from app.application.query_understanding import (
    ParameterConcept,
    ParameterConceptOutcome,
    ParameterResolutionStatus,
    ParameterResolver,
)
from app.domain.hydrology import ParameterCatalogCandidate, ParameterKey
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import PortUnavailableError


def key(code: str) -> ParameterKey:
    return ParameterKey(code_system="usgs", code=code)


def concept(
    value: str,
    *,
    rationale: str = "Needed to answer the user's water question.",
    explicitly_named: bool = True,
) -> ParameterConcept:
    return ParameterConcept(
        concept=value,
        rationale=rationale,
        explicitly_named=explicitly_named,
    )


class FakeParameterCatalog:
    def __init__(
        self,
        results: dict[str, tuple[ParameterCatalogCandidate, ...]] | None = None,
    ) -> None:
        self.results = results or {}
        self.calls: list[str] = []

    async def find_candidates(
        self,
        requested_concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]:
        self.calls.append(requested_concept)
        return self.results.get(requested_concept, ())


class UnavailableParameterCatalog:
    async def find_candidates(
        self,
        requested_concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]:
        raise PortUnavailableError("parameter catalog unavailable")


def resolve(
    concepts: tuple[ParameterConcept, ...],
    catalog: FakeParameterCatalog | None = None,
):
    active_catalog = catalog or FakeParameterCatalog()
    result = asyncio.run(
        ParameterResolver(
            load_parameter_capability_registry(),
            active_catalog,
        ).resolve(concepts)
    )
    return result, active_catalog


def candidate(code: str, name: str, score: float) -> ParameterCatalogCandidate:
    return ParameterCatalogCandidate(
        key=key(code),
        canonical_name=name,
        score=score,
    )


def test_canonical_and_chinese_aliases_resolve_without_catalog_discovery() -> None:
    requested = concept(
        " 水温 ",
        rationale="The user explicitly asked whether the water is warm.",
    )

    result, catalog = resolve((requested,))

    assert result.status is ParameterResolutionStatus.SUCCESS
    assert tuple(item.key.code for item in result.target_parameters) == ("00010",)
    assert tuple(item.key.code for item in result.supporting_parameters) == ("00060",)
    assert result.target_parameters[0].concepts == (requested,)
    assert catalog.calls == []


def test_duplicate_alias_is_ambiguous_without_using_candidate_scores() -> None:
    result, catalog = resolve((concept("turbidity"),))

    assert result.status is ParameterResolutionStatus.PARAMETER_AMBIGUOUS
    assert result.target_parameters == ()
    assert result.supporting_parameters == ()
    assert result.concept_resolutions[0].outcome is ParameterConceptOutcome.AMBIGUOUS
    assert tuple(
        item.code for item in result.concept_resolutions[0].candidate_keys
    ) == ("61028", "63680")
    assert catalog.calls == []


def test_catalog_can_resolve_a_few_shot_uncovered_inferred_concept() -> None:
    requested = concept(
        "Can aquatic animals breathe here?",
        rationale="Dissolved oxygen supports aquatic respiration.",
        explicitly_named=False,
    )
    catalog = FakeParameterCatalog(
        {
            requested.concept: (
                candidate("00300", "Dissolved oxygen", 0.15),
            )
        }
    )

    result, _ = resolve((requested,), catalog)

    assert result.status is ParameterResolutionStatus.SUCCESS
    assert result.target_parameters[0].key == key("00300")
    assert result.target_parameters[0].concepts == (requested,)
    assert result.concept_resolutions[0].catalog_candidates[0].score == 0.15


def test_catalog_score_never_selects_one_of_multiple_substantive_candidates() -> None:
    requested = concept("cloudy water", explicitly_named=False)
    catalog = FakeParameterCatalog(
        {
            requested.concept: (
                candidate("63680", "Turbidity, Formazin Nephelometric", 0.99),
                candidate("61028", "Turbidity, field", 0.01),
            )
        }
    )

    result, _ = resolve((requested,), catalog)

    assert result.status is ParameterResolutionStatus.PARAMETER_AMBIGUOUS
    assert tuple(
        item.code for item in result.concept_resolutions[0].candidate_keys
    ) == ("61028", "63680")


@pytest.mark.parametrize(
    "catalog_results",
    [(), (candidate("99999", "Catalog only parameter", 1.0),)],
)
def test_missing_or_catalog_only_capability_is_unsupported(catalog_results) -> None:
    requested = concept("unsupported concept", explicitly_named=False)
    catalog = FakeParameterCatalog({requested.concept: catalog_results})

    result, _ = resolve((requested,), catalog)

    assert result.status is ParameterResolutionStatus.PARAMETER_UNSUPPORTED
    assert result.concept_resolutions[0].concept == requested
    assert result.target_parameters == ()


def test_catalog_unavailability_is_not_converted_to_an_unsupported_result() -> None:
    with pytest.raises(PortUnavailableError, match="catalog unavailable"):
        asyncio.run(
            ParameterResolver(
                load_parameter_capability_registry(),
                UnavailableParameterCatalog(),
            ).resolve((concept("unlisted concept"),))
        )


def test_empty_parameter_list_returns_required_without_catalog_calls() -> None:
    result, catalog = resolve(())

    assert result.status is ParameterResolutionStatus.PARAMETER_REQUIRED
    assert result.concept_resolutions == ()
    assert catalog.calls == []


def test_any_failed_concept_fails_the_whole_plan_without_dropping_diagnostics() -> None:
    supported = concept("Dissolved oxygen", rationale="Explicit oxygen request.")
    unsupported = concept("moonlight chemistry", explicitly_named=False)

    result, _ = resolve((supported, unsupported))

    assert result.status is ParameterResolutionStatus.PARAMETER_UNSUPPORTED
    assert result.target_parameters == ()
    assert result.supporting_parameters == ()
    assert tuple(item.concept for item in result.concept_resolutions) == (
        supported,
        unsupported,
    )
    assert tuple(item.outcome for item in result.concept_resolutions) == (
        ParameterConceptOutcome.UNIQUE,
        ParameterConceptOutcome.UNSUPPORTED,
    )


def test_duplicate_targets_are_deduplicated_but_all_rationales_are_preserved() -> None:
    first = concept("water temperature", rationale="Explicit temperature request.")
    second = concept(
        "水温",
        rationale="Temperature also affects habitat suitability.",
        explicitly_named=False,
    )

    result, _ = resolve((first, second))

    assert result.status is ParameterResolutionStatus.SUCCESS
    assert len(result.target_parameters) == 1
    assert result.target_parameters[0].concepts == (first, second)


def test_one_and_six_unique_targets_succeed_while_seven_fail_as_a_unit() -> None:
    names = (
        "Temperature, water",
        "Dissolved oxygen",
        "Specific conductance at 25C",
        "pH",
        "Nitrate, filtered",
        "Phosphorus, unfiltered",
        "Salinity",
    )

    one, _ = resolve(tuple(concept(name) for name in names[:1]))
    six, _ = resolve(tuple(concept(name) for name in names[:6]))
    seven, _ = resolve(tuple(concept(name) for name in names))

    assert one.status is ParameterResolutionStatus.SUCCESS
    assert len(one.target_parameters) == 1
    assert six.status is ParameterResolutionStatus.SUCCESS
    assert len(six.target_parameters) == 6
    assert seven.status is ParameterResolutionStatus.TOO_MANY_PARAMETERS
    assert len(seven.concept_resolutions) == 7
    assert seven.target_parameters == ()
    assert seven.supporting_parameters == ()


def test_discharge_is_not_duplicated_when_it_is_a_user_target() -> None:
    result, _ = resolve((concept("Discharge"),))

    assert result.status is ParameterResolutionStatus.SUCCESS
    assert tuple(item.key.code for item in result.target_parameters) == ("00060",)
    assert result.supporting_parameters == ()


@pytest.mark.parametrize("maximum", [True, 0, 7, 1.5])
def test_resolver_rejects_invalid_target_limits(maximum: object) -> None:
    with pytest.raises(ValueError, match="max_target_parameters"):
        ParameterResolver(
            load_parameter_capability_registry(),
            FakeParameterCatalog(),
            max_target_parameters=maximum,
        )
