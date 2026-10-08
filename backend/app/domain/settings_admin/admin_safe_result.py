"""Closed Admin result union."""

from typing import Annotated
from pydantic import Field
from .history_cleanup_result import HistoryCleanupResult
from .profile_handoff_result import ProfileHandoffResult
from .retry_failed_jobs_result import RetryFailedJobsResult
from .generation_deletion_result import GenerationDeletionResult

AdminSafeResult = Annotated[
    HistoryCleanupResult | ProfileHandoffResult | RetryFailedJobsResult | GenerationDeletionResult,
    Field(discriminator="kind"),
]
