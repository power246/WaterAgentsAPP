from app.domain.common import DomainEnum


class InferenceMode(DomainEnum):
    GAP_FILLING = "gap_filling"
    PREDICTION = "prediction"
    MIXED = "mixed"
