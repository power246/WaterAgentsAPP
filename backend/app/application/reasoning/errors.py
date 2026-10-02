from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.reasoning.draft_models import DraftValidationIssue
    from app.application.reasoning.validation_repair import ValidationExchangeAudit


class ReasoningBoundaryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WaterbodyTaskValidationError(ReasoningBoundaryError):
    pass


class SealedEvidenceValidationError(ReasoningBoundaryError):
    pass


class PlanningLoopError(ReasoningBoundaryError):
    pass


class ResultSkeletonValidationError(ReasoningBoundaryError):
    pass


class ReasoningPlanError(ReasoningBoundaryError):
    pass


class ReasoningPlanTooLargeError(ReasoningPlanError):
    def __init__(self) -> None:
        super().__init__("reasoning_plan_too_large")


class DraftValidationError(ReasoningBoundaryError):
    def __init__(self, issues: tuple["DraftValidationIssue", ...]) -> None:
        self.issues = issues
        super().__init__("reasoning_draft_invalid")


class ValidationRepairFailedError(ReasoningBoundaryError):
    def __init__(
        self,
        audit: "ValidationExchangeAudit",
        *,
        cause_code: str,
    ) -> None:
        self.audit = audit
        self.cause_code = cause_code
        super().__init__("waterbody_model_repair_failed")


class AllCellsBlockedError(ResultSkeletonValidationError):
    def __init__(self) -> None:
        super().__init__("all_reasoning_cells_blocked")
