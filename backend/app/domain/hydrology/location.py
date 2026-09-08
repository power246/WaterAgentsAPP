from pydantic import Field

from app.domain.common import DomainFloat, DomainModel


class GeoPoint(DomainModel):
    latitude: DomainFloat = Field(ge=-90, le=90)
    longitude: DomainFloat = Field(ge=-180, le=180)
