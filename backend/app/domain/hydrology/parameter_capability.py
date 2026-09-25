from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.evidence_rule_id import EvidenceRuleId
from app.domain.hydrology.parameter_concept_normalization import (
    normalize_parameter_concept,
)
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.parameter_role import ParameterRole


class ParameterCapability(DomainModel):
    key: ParameterKey
    canonical_name: NonEmptyString
    canonical_unit: NonEmptyString
    official_name: NonEmptyString
    official_unit: NonEmptyString
    official_group: NonEmptyString
    medium: NonEmptyString | None
    sample_fraction: NonEmptyString | None
    aliases_zh: tuple[NonEmptyString, ...]
    aliases_en: tuple[NonEmptyString, ...]
    roles: tuple[ParameterRole, ...]
    availability_providers: tuple[ParameterProvider, ...]
    observation_providers: tuple[ParameterProvider, ...]
    allowed_indirect_evidence_rules: tuple[EvidenceRuleId, ...]
    fixture_identity: NonEmptyString
    resolution_enabled: StrictBool

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        if self.key.code_system != "usgs" or (
            len(self.key.code) != 5 or not self.key.code.isdecimal()
        ):
            raise ValueError("parameter capability key must be usgs:<five digits>")
        for field_name in (
            "aliases_zh",
            "aliases_en",
            "roles",
            "availability_providers",
            "observation_providers",
            "allowed_indirect_evidence_rules",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            ensure_unique_tuple(values, field_name=field_name)
        if ParameterRole.TARGET not in self.roles:
            raise ValueError("every capability must support the target role")
        canonical = normalize_parameter_concept(self.canonical_name)
        aliases = tuple(
            normalize_parameter_concept(alias)
            for alias in (*self.aliases_zh, *self.aliases_en)
        )
        if canonical in aliases:
            raise ValueError("aliases must not duplicate canonical_name")
        if len(aliases) != len(set(aliases)):
            raise ValueError("aliases must be unique within a capability")
        if not any(
            "\u4e00" <= character <= "\u9fff"
            for alias in self.aliases_zh
            for character in alias
        ):
            raise ValueError("aliases_zh must contain a Chinese alias")
        if not any(
            character.isascii() and character.isalpha()
            for alias in self.aliases_en
            for character in alias
        ):
            raise ValueError("aliases_en must contain an English alias")
        return self
