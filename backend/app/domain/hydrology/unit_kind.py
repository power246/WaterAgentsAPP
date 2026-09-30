from app.domain.domain_enum import DomainEnum


class UnitKind(DomainEnum):
    LINEAR = "linear"
    AFFINE = "affine"
    IDENTITY_ONLY = "identity_only"
