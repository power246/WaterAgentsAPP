"""M9 domain contract."""

from typing import Literal
from app.domain.common import DomainModel
from .count import Count
from .admin_action_kind import AdminActionKind


class RetryFailedJobsResult(DomainModel):
    kind: Literal[AdminActionKind.RETRY_FAILED_JOBS] = AdminActionKind.RETRY_FAILED_JOBS
    retried_jobs: Count
