from app.domain.guidelines.document_details import GuidelineDocumentDetails


class GuidelineServiceError(Exception):
    def __init__(self, code: str, status: int, *, current: GuidelineDocumentDetails | None = None) -> None:
        super().__init__("The guideline request could not be completed.")
        self.code = code
        self.status = status
        self.current = current
