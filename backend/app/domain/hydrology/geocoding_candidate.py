from app.domain.common import DomainModel
from app.domain.hydrology.administrative_location import AdministrativeLocation
from app.domain.hydrology.location import GeoPoint


class GeocodingCandidate(DomainModel):
    point: GeoPoint
    administrative_location: AdministrativeLocation
