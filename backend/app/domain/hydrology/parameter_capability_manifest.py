from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.aggregation_profile import AggregationProfile
from app.domain.hydrology.coverage_profile import CoverageProfile
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.quality_profile import QualityProfile
from app.domain.hydrology.unit_definition import UnitDefinition


class ParameterCapabilityManifest(DomainModel):
    schema_version: Literal[1]
    catalog_key_set_sha256: NonEmptyString
    manifest_digest_sha256: NonEmptyString
    unit_definitions: tuple[UnitDefinition, ...]
    quality_profiles: tuple[QualityProfile, ...]
    aggregation_profiles: tuple[AggregationProfile, ...]
    coverage_profiles: tuple[CoverageProfile, ...]
    capabilities: tuple[ParameterCapability, ...]
