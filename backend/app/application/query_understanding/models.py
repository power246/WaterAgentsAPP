from __future__ import annotations

from typing import Annotated, Self

from pydantic import StringConstraints, model_validator

from app.domain import DomainEnum, DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology import ParameterCatalogCandidate, ParameterKey

ShortExplanation = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ParameterConcept(DomainModel):
    concept: NonEmptyString
    rationale: ShortExplanation
    explicitly_named: bool


class ParameterConceptOutcome(DomainEnum):
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class ParameterResolutionStatus(DomainEnum):
    SUCCESS = "success"
    PARAMETER_REQUIRED = "parameter_required"
    PARAMETER_AMBIGUOUS = "parameter_ambiguous"
    PARAMETER_UNSUPPORTED = "parameter_unsupported"
    TOO_MANY_PARAMETERS = "too_many_parameters"


class SupportingParameterReason(DomainEnum):
    CAPABILITY_REQUIRED = "capability_required"


class ParameterConceptResolution(DomainModel):
    concept: ParameterConcept
    outcome: ParameterConceptOutcome
    candidate_keys: tuple[ParameterKey, ...]
    catalog_candidates: tuple[ParameterCatalogCandidate, ...] = ()
    resolved_key: ParameterKey | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        ensure_unique_tuple(self.candidate_keys, field_name="candidate_keys")
        if self.outcome is ParameterConceptOutcome.UNIQUE:
            if len(self.candidate_keys) != 1 or self.resolved_key != self.candidate_keys[0]:
                raise ValueError("unique resolution requires one resolved candidate")
        elif self.resolved_key is not None:
            raise ValueError("failed concept resolution cannot have resolved_key")
        if (
            self.outcome is ParameterConceptOutcome.AMBIGUOUS
            and len(self.candidate_keys) < 2
        ):
            raise ValueError("ambiguous resolution requires multiple candidates")
        return self


class ResolvedTargetParameter(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    concepts: tuple[ParameterConcept, ...]

    @model_validator(mode="after")
    def validate_concepts(self) -> Self:
        if not self.concepts:
            raise ValueError("resolved target must preserve at least one concept")
        return self


class SupportingParameter(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    reason: SupportingParameterReason


class ParameterResolutionResult(DomainModel):
    status: ParameterResolutionStatus
    concept_resolutions: tuple[ParameterConceptResolution, ...]
    target_parameters: tuple[ResolvedTargetParameter, ...]
    supporting_parameters: tuple[SupportingParameter, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is ParameterResolutionStatus.SUCCESS:
            if not self.target_parameters or not self.concept_resolutions:
                raise ValueError("successful resolution requires target parameters")
            if any(
                item.outcome is not ParameterConceptOutcome.UNIQUE
                for item in self.concept_resolutions
            ):
                raise ValueError("successful resolution requires unique concepts")
        elif self.target_parameters or self.supporting_parameters:
            raise ValueError("failed resolution cannot expose an executable plan")
        if (
            self.status is ParameterResolutionStatus.PARAMETER_REQUIRED
            and self.concept_resolutions
        ):
            raise ValueError("parameter_required cannot include concepts")
        target_keys = tuple(item.key for item in self.target_parameters)
        support_keys = tuple(item.key for item in self.supporting_parameters)
        ensure_unique_tuple(target_keys, field_name="target_parameters")
        ensure_unique_tuple(support_keys, field_name="supporting_parameters")
        if set(target_keys) & set(support_keys):
            raise ValueError("target and supporting parameters must not overlap")
        return self
