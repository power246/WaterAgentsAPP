from app.domain.common import DomainEnum


class CanonicalIntentToken(DomainEnum):
    ANALYZE_CURRENT_STATE = "analyze_current_state"
    FILL_GAP = "fill_gap"
    PREDICT = "predict"
    REQUIRES_DISCHARGE_SUPPORT = "requires_discharge_support"
