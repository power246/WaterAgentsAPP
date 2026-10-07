from app.domain.common import DomainEnum


class RAGIndexGenerationStatus(DomainEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"
