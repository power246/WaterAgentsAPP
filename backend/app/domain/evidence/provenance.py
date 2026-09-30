from app.domain.common import DomainModel, NonEmptyString


class Provenance(DomainModel):
    operation_id: NonEmptyString | None = None
    registry_digest: NonEmptyString | None = None
    request_identity: NonEmptyString | None = None
    page_identity: NonEmptyString | None = None
    chunk_identity: NonEmptyString | None = None
    provider_series_id: NonEmptyString | None = None
    provider_record_id: NonEmptyString | None = None
    source_timezone: NonEmptyString | None = None
    source_utc_offset: NonEmptyString | None = None
