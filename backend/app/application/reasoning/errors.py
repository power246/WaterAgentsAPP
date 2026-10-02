class ReasoningBoundaryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WaterbodyTaskValidationError(ReasoningBoundaryError):
    pass


class SealedEvidenceValidationError(ReasoningBoundaryError):
    pass


class ResultSkeletonValidationError(ReasoningBoundaryError):
    pass


class AllCellsBlockedError(ResultSkeletonValidationError):
    def __init__(self) -> None:
        super().__init__("all_reasoning_cells_blocked")
