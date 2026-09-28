from app.domain.common import DomainModel, NonEmptyString


class AdministrativeLocation(DomainModel):
    country_code: NonEmptyString
    subdivision_code: NonEmptyString
    display_name: NonEmptyString | None = None
    provider_identity: NonEmptyString
