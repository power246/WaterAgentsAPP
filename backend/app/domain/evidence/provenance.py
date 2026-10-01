from typing import Annotated

from pydantic import Field
from pydantic import field_validator

from app.domain.common import CanonicalDecimal, DomainFloat, DomainModel, NonEmptyString
from app.domain.common import ensure_unique_tuple
from app.domain.hydrology.location import GeoPoint


class Provenance(DomainModel):
    operation_id: NonEmptyString | None = None
    registry_digest: NonEmptyString | None = None
    request_identity: NonEmptyString | None = None
    page_identity: NonEmptyString | None = None
    chunk_identity: NonEmptyString | None = None
    provider_series_id: NonEmptyString | None = None
    provider_record_id: NonEmptyString | None = None
    discovery_identities: tuple[NonEmptyString, ...] = ()
    source_timezone: NonEmptyString | None = None
    source_utc_offset: NonEmptyString | None = None
    facility_agent_radius_miles: CanonicalDecimal | None = None
    facility_provider_radius_miles: CanonicalDecimal | None = None
    facility_sample_centers: tuple[GeoPoint, ...] = ()
    facility_request_identities: tuple[NonEmptyString, ...] = ()
    facility_deduplicated_count: Annotated[int, Field(strict=True, ge=0)] = 0
    facility_final_distance_km: Annotated[DomainFloat, Field(ge=0)] | None = None

    @field_validator("discovery_identities")
    @classmethod
    def validate_discovery_identities(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="discovery identities")
