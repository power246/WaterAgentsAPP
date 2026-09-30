from app.domain.common import DomainEnum


class CollectionCompletionContract(DomainEnum):
    OFFICIAL_NEXT_CHAIN = "official_next_chain"
    COMPLETE_CSV_STREAM = "complete_csv_stream"
    SINGLE_RESPONSE = "single_response"
