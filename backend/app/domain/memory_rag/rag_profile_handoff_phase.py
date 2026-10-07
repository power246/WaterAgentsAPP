from app.domain.domain_enum import DomainEnum


class RAGProfileHandoffPhase(DomainEnum):
    BUILD_REQUIRED = "build_required"
    BUILDING = "building"
    READER_DRAIN = "reader_drain"
    CREDENTIAL_CLEANUP_READY = "credential_cleanup_ready"
