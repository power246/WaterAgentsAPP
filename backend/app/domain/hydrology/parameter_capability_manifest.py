from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.evidence_rule_id import EvidenceRuleId
from app.domain.hydrology.parameter_capability import ParameterCapability


class ParameterCapabilityManifest(DomainModel):
    schema_version: Literal[1]
    catalog_key_set_sha256: NonEmptyString
    evidence_rule_ids: tuple[EvidenceRuleId, ...]
    capabilities: tuple[ParameterCapability, ...]
