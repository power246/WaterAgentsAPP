import asyncio

from app.application.analysis_runs.workflow import WorkflowStopped
from app.domain import SafeEvidenceError
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    UnsupportedPortVersionError,
)


class EvidenceToolError(Exception):
    code = "evidence_provider_unavailable"
    public_message = "The evidence source is unavailable."
    retryable = False


class EvidenceScopeViolationError(EvidenceToolError):
    code = "evidence_scope_violation"
    public_message = "The evidence request is outside this analysis scope."


class EvidenceTimeOutOfRangeError(EvidenceToolError):
    code = "evidence_time_out_of_range"
    public_message = "The evidence request time is outside the allowed range."


class EvidenceStationNotAuthorizedError(EvidenceToolError):
    code = "evidence_station_not_authorized"
    public_message = "The evidence station is not authorized for this waterbody."


class EvidenceToolBudgetExceededError(EvidenceToolError):
    code = "evidence_tool_budget_exceeded"
    public_message = "The evidence tool budget has been exhausted."


class EvidenceToolDeadlineExceededError(EvidenceToolError):
    code = "evidence_provider_timeout"
    public_message = "The evidence operation exceeded its deadline."
    retryable = True


class EvidenceToolLimitExceededError(EvidenceToolError):
    code = "evidence_provider_response_too_large"
    public_message = "The evidence response exceeded a safety limit."


class EvidenceCollectionIncompleteError(EvidenceToolError):
    code = "evidence_collection_incomplete"
    public_message = "The evidence collection could not be proven complete."
    retryable = True


class EvidenceUnitUnresolvedError(EvidenceToolError):
    code = "evidence_unit_unresolved"
    public_message = "The evidence unit could not be resolved."


class EvidenceAggregationNotAllowedError(EvidenceToolError):
    code = "evidence_aggregation_not_allowed"
    public_message = "The requested evidence aggregation is not allowed."


class WaterbodyGeometryUnavailableError(EvidenceToolError):
    code = "waterbody_geometry_unavailable"
    public_message = "The waterbody geometry is unavailable."


class FacilityRadiusTooSmallForBudgetError(EvidenceToolError):
    code = "facility_radius_too_small_for_budget"
    public_message = "The selected facility radius requires too many requests."


class EvidenceProducerUnavailableError(EvidenceToolError):
    code = "evidence_producer_unavailable"
    public_message = "The requested evidence producer is unavailable."


def map_evidence_error(error: BaseException) -> SafeEvidenceError:
    if isinstance(error, (asyncio.CancelledError, WorkflowStopped)):
        raise error
    if isinstance(error, EvidenceToolError):
        return SafeEvidenceError(
            code=error.code,
            message=error.public_message,
            retryable=error.retryable,
        )

    mapping: tuple[tuple[type[BaseException], str, str, bool], ...] = (
        (
            PortAuthenticationError,
            "evidence_provider_authentication_failed",
            "The evidence provider could not authenticate the request.",
            False,
        ),
        (
            PortRateLimitError,
            "evidence_provider_rate_limited",
            "The evidence provider rate limit was reached.",
            True,
        ),
        (
            PortTimeoutError,
            "evidence_provider_timeout",
            "The evidence provider timed out.",
            True,
        ),
        (
            PortResponseLimitError,
            "evidence_provider_response_too_large",
            "The evidence provider response exceeded a safety limit.",
            False,
        ),
        (
            (InvalidPortResponseError, UnsupportedPortVersionError),
            "evidence_provider_response_invalid",
            "The evidence provider response was invalid.",
            False,
        ),
        (
            (PortUnavailableError, PortHttpError, PortConfigurationError, PortError),
            "evidence_provider_unavailable",
            "The evidence provider is unavailable.",
            True,
        ),
    )
    for error_type, code, message, retryable in mapping:
        if isinstance(error, error_type):
            return SafeEvidenceError(code=code, message=message, retryable=retryable)
    return SafeEvidenceError(
        code="evidence_provider_response_invalid",
        message="The evidence operation failed safely.",
        retryable=False,
    )
