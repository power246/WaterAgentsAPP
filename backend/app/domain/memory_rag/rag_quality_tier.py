from app.domain.common import DomainEnum


class RAGQualityTier(DomainEnum):
    VALID = "valid"
    SUSPICIOUS = "suspicious"
