from app.domain.common import DomainModel, NonEmptyString


class HistoricalCaseRef(DomainModel):
    generation_id: NonEmptyString
    case_id: NonEmptyString
    run_id: NonEmptyString
    session_id: NonEmptyString
    target_comid: NonEmptyString
    analysis_context_item_id: NonEmptyString
    result_item_id: NonEmptyString
