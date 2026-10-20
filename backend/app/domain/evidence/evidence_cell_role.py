from app.domain.domain_enum import DomainEnum


class EvidenceCellRole(DomainEnum):
    TARGET = "target"
    REQUIRED_SUPPORT = "required_support"
    METHOD_INPUT = "method_input"
