from __future__ import annotations

from app.application.query_understanding.models import (
    ParameterConcept,
    ParameterConceptOutcome,
    ParameterConceptResolution,
    ParameterResolutionResult,
    ParameterResolutionStatus,
    ResolvedTargetParameter,
    SupportingParameter,
    SupportingParameterReason,
)
from app.domain.hydrology import (
    ParameterCapability,
    ParameterCapabilityRegistry,
    ParameterCatalogCandidate,
    ParameterKey,
)
from app.ports.parameter_catalog import ParameterCatalogPort


class ParameterResolver:
    def __init__(
        self,
        registry: ParameterCapabilityRegistry,
        catalog: ParameterCatalogPort,
        *,
        max_target_parameters: int = 6,
    ) -> None:
        if (
            isinstance(max_target_parameters, bool)
            or not isinstance(max_target_parameters, int)
            or not 1 <= max_target_parameters <= 6
        ):
            raise ValueError("max_target_parameters must be an integer from 1 to 6")
        self._registry = registry
        self._catalog = catalog
        self._max_target_parameters = max_target_parameters

    async def resolve(
        self,
        concepts: tuple[ParameterConcept, ...],
    ) -> ParameterResolutionResult:
        if not concepts:
            return self._failure(ParameterResolutionStatus.PARAMETER_REQUIRED, ())

        resolutions: list[ParameterConceptResolution] = []
        resolved: list[tuple[ParameterConcept, ParameterCapability]] = []
        for concept in concepts:
            resolution, capability = await self._resolve_concept(concept)
            resolutions.append(resolution)
            if capability is not None:
                resolved.append((concept, capability))

        if any(
            item.outcome is ParameterConceptOutcome.AMBIGUOUS
            for item in resolutions
        ):
            return self._failure(
                ParameterResolutionStatus.PARAMETER_AMBIGUOUS,
                tuple(resolutions),
            )
        if any(
            item.outcome is ParameterConceptOutcome.UNSUPPORTED
            for item in resolutions
        ):
            return self._failure(
                ParameterResolutionStatus.PARAMETER_UNSUPPORTED,
                tuple(resolutions),
            )

        concepts_by_key: dict[ParameterKey, list[ParameterConcept]] = {}
        capability_by_key: dict[ParameterKey, ParameterCapability] = {}
        for concept, capability in resolved:
            capability_by_key[capability.key] = capability
            concepts_by_key.setdefault(capability.key, []).append(concept)
        if len(concepts_by_key) > self._max_target_parameters:
            return self._failure(
                ParameterResolutionStatus.TOO_MANY_PARAMETERS,
                tuple(resolutions),
            )

        target_parameters = tuple(
            ResolvedTargetParameter(
                key=key,
                canonical_name=capability_by_key[key].canonical_name,
                canonical_unit=capability_by_key[key].canonical_unit,
                concepts=tuple(concepts_by_key[key]),
            )
            for key in concepts_by_key
        )
        target_keys = set(concepts_by_key)
        supporting_parameters = tuple(
            SupportingParameter(
                key=capability.key,
                canonical_name=capability.canonical_name,
                canonical_unit=capability.canonical_unit,
                reason=SupportingParameterReason.CAPABILITY_REQUIRED,
            )
            for capability in self._registry.required_support
            if capability.key not in target_keys
        )
        return ParameterResolutionResult(
            status=ParameterResolutionStatus.SUCCESS,
            concept_resolutions=tuple(resolutions),
            target_parameters=target_parameters,
            supporting_parameters=supporting_parameters,
        )

    async def _resolve_concept(
        self,
        concept: ParameterConcept,
    ) -> tuple[ParameterConceptResolution, ParameterCapability | None]:
        exact_matches = self._registry.exact_matches(concept.concept)
        if exact_matches:
            ordered_matches = tuple(
                sorted(
                    exact_matches,
                    key=lambda item: (item.key.code_system, item.key.code),
                )
            )
            keys = tuple(item.key for item in ordered_matches)
            if len(ordered_matches) == 1:
                return (
                    ParameterConceptResolution(
                        concept=concept,
                        outcome=ParameterConceptOutcome.UNIQUE,
                        candidate_keys=keys,
                        resolved_key=keys[0],
                    ),
                    ordered_matches[0],
                )
            return (
                ParameterConceptResolution(
                    concept=concept,
                    outcome=ParameterConceptOutcome.AMBIGUOUS,
                    candidate_keys=keys,
                ),
                None,
            )

        catalog_candidates = self._deduplicate_catalog_candidates(
            await self._catalog.find_candidates(concept.concept)
        )
        candidate_keys = tuple(candidate.key for candidate in catalog_candidates)
        if len(candidate_keys) > 1:
            return (
                ParameterConceptResolution(
                    concept=concept,
                    outcome=ParameterConceptOutcome.AMBIGUOUS,
                    candidate_keys=candidate_keys,
                    catalog_candidates=catalog_candidates,
                ),
                None,
            )
        if len(candidate_keys) == 1:
            capability = self._registry.get(candidate_keys[0])
            if capability is not None:
                return (
                    ParameterConceptResolution(
                        concept=concept,
                        outcome=ParameterConceptOutcome.UNIQUE,
                        candidate_keys=candidate_keys,
                        catalog_candidates=catalog_candidates,
                        resolved_key=candidate_keys[0],
                    ),
                    capability,
                )
        return (
            ParameterConceptResolution(
                concept=concept,
                outcome=ParameterConceptOutcome.UNSUPPORTED,
                candidate_keys=candidate_keys,
                catalog_candidates=catalog_candidates,
            ),
            None,
        )

    @staticmethod
    def _deduplicate_catalog_candidates(
        candidates: tuple[ParameterCatalogCandidate, ...],
    ) -> tuple[ParameterCatalogCandidate, ...]:
        by_key: dict[ParameterKey, ParameterCatalogCandidate] = {}
        for candidate in candidates:
            current = by_key.get(candidate.key)
            if current is None or candidate.score > current.score:
                by_key[candidate.key] = candidate
        return tuple(
            by_key[key]
            for key in sorted(by_key, key=lambda item: (item.code_system, item.code))
        )

    @staticmethod
    def _failure(
        status: ParameterResolutionStatus,
        resolutions: tuple[ParameterConceptResolution, ...],
    ) -> ParameterResolutionResult:
        return ParameterResolutionResult(
            status=status,
            concept_resolutions=resolutions,
            target_parameters=(),
            supporting_parameters=(),
        )
