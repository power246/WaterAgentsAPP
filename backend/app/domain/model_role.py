from app.domain.domain_enum import DomainEnum


class ModelRole(DomainEnum):
    QUERY_PARSER = "query_parser"
    WATERBODY = "waterbody"
    EMBEDDING = "embedding"
