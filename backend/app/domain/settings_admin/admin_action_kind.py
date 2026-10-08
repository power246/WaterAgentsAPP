"""M9 domain contract."""

from app.domain.domain_enum import DomainEnum


class AdminActionKind(DomainEnum):
    HISTORY_CLEANUP = "history_cleanup"
    PROFILE_HANDOFF = "profile_handoff"
    RETRY_FAILED_JOBS = "retry_failed_jobs"
    GENERATION_DELETION = "generation_deletion"
