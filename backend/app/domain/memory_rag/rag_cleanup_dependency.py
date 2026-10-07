from app.domain.common import DomainModel, NonEmptyString


class RAGCleanupDependency(DomainModel):
    link_id: NonEmptyString
    source_generation_id: NonEmptyString
    source_case_id: NonEmptyString
    source_run_id: NonEmptyString
    source_result_item_id: NonEmptyString
    consumer_run_id: NonEmptyString
    consumer_item_id: NonEmptyString
