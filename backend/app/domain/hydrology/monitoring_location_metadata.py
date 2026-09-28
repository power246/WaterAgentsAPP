from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology.location import GeoPoint


class MonitoringLocationMetadata(DomainModel):
    station_id: NonEmptyString
    agency_code: NonEmptyString
    name: NonEmptyString
    location: GeoPoint
    provider_identity: NonEmptyString
