from pydantic import Field

from app.domain.common import DomainModel


class GeoPoint(DomainModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
