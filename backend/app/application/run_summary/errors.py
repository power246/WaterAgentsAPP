from __future__ import annotations


class RunSummaryError(RuntimeError):
    code = "run_summary_failed"
    retryable = False


class SummaryInputTooLargeError(RunSummaryError):
    code = "summary_input_too_large"


class RunSummaryValidationError(RunSummaryError):
    code = "run_summary_invalid"

    def __init__(self, issues: tuple[tuple[str, str], ...]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("run summary output did not satisfy its contract")
