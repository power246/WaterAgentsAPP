"""Resolve bounded query unit aliases inside the sealed parameter scope."""

from typing import Annotated

from pydantic import StringConstraints

from app.domain.common import DomainModel
from app.domain.hydrology import ParameterCapabilityRegistry, ParameterKey
from app.domain.hydrology.unit_kind import UnitKind
from app.domain.settings_admin import UnitPolicyProjection, UnitPreference


UnitAlias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
ParameterReference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
UnitRationale = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ParsedUnitIntent(DomainModel):
    raw_unit_alias: UnitAlias
    parameter_reference: ParameterReference | None = None
    rationale: UnitRationale | None = None


class UnitPreferenceIssue(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnitPreferenceResolver:
    def __init__(self, registry: ParameterCapabilityRegistry) -> None:
        self._registry = registry

    def resolve(
        self, *, intents: tuple[ParsedUnitIntent, ...],
        scoped_parameters: tuple[ParameterKey, ...],
        policy: UnitPolicyProjection | None,
    ) -> tuple[UnitPreference, ...]:
        registry = self._registry
        if policy is not None and policy.registry_digest != registry.manifest_digest:
            raise UnitPreferenceIssue("unit_registry_changed")
        scoped = set(scoped_parameters)
        selected: dict[ParameterKey, UnitPreference] = {}
        for intent in intents:
            unit = registry.resolve_unit(intent.raw_unit_alias)
            if unit is None:
                raise UnitPreferenceIssue("unit_unknown")
            if intent.parameter_reference is not None:
                matches = registry.exact_matches(intent.parameter_reference)
                if len(matches) != 1:
                    raise UnitPreferenceIssue(
                        "unit_parameter_ambiguous" if matches else "unit_parameter_unknown"
                    )
                candidates = (matches[0].key,)
                if candidates[0] not in scoped:
                    raise UnitPreferenceIssue("unit_parameter_out_of_scope")
            else:
                candidates = tuple(sorted(scoped, key=lambda key: (key.code_system, key.code)))
            compatible = []
            for key in candidates:
                capability = registry.get(key)
                if capability is None:
                    raise UnitPreferenceIssue("unit_parameter_unknown")
                canonical = registry.unit_definitions[capability.unit_policy_id]
                if canonical.dimension_id != unit.dimension_id:
                    if intent.parameter_reference is not None:
                        raise UnitPreferenceIssue("unit_dimension_mismatch")
                    continue
                if (canonical.kind is UnitKind.IDENTITY_ONLY or unit.kind is UnitKind.IDENTITY_ONLY) and canonical.unit_id != unit.unit_id:
                    raise UnitPreferenceIssue("unit_not_applicable")
                compatible.append(key)
            if not compatible:
                raise UnitPreferenceIssue("unit_not_applicable")
            for key in compatible:
                if key in selected:
                    raise UnitPreferenceIssue("unit_ambiguous")
                selected[key] = UnitPreference(
                    parameter=key, target_unit_id=unit.unit_id,
                    registry_digest=registry.manifest_digest,
                    source="session_override",
                )
        if policy is not None:
            for entry in policy.entries:
                if entry.parameter not in scoped or entry.parameter in selected:
                    continue
                capability = registry.get(entry.parameter)
                if capability is None:
                    raise UnitPreferenceIssue("unit_parameter_unknown")
                if entry.target_unit_id != capability.unit_policy_id:
                    selected[entry.parameter] = UnitPreference(
                        parameter=entry.parameter, target_unit_id=entry.target_unit_id,
                        registry_digest=registry.manifest_digest,
                        source="global_policy",
                    )
        return tuple(selected[key] for key in sorted(
            selected, key=lambda key: (key.code_system, key.code)
        ))
