class PortError(Exception):
    """Base error for failures while using an external capability."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        operation: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.status_code = status_code


class PortUnavailableError(PortError):
    """Raised when an external capability cannot serve the request."""


class PortTimeoutError(PortError):
    """Raised when an external capability does not respond in time."""


class InvalidPortResponseError(PortError):
    """Raised when an external response violates the port contract."""


class PortAuthenticationError(PortUnavailableError):
    """Raised when provider credentials are absent, rejected, or unauthorized."""


class PortConfigurationError(PortError):
    """Raised when an external capability is configured incorrectly."""


class PortHttpError(PortUnavailableError):
    """Raised for a provider HTTP response outside the explicit taxonomy."""


class PortRateLimitError(PortUnavailableError):
    """Raised when a provider rejects a request because of rate limiting."""


class PortResponseLimitError(InvalidPortResponseError):
    """Raised when a bounded provider response or pagination limit is exceeded."""


class UnsupportedPortVersionError(InvalidPortResponseError):
    """Raised when a provider API version is not supported by the adapter."""


class EmbeddingCancelledError(PortError):
    """Raised when an embedding call is cancelled by its explicit token."""


class WaterbodyModelPortError(PortError):
    """Base class for safe, provider-neutral waterbody model failures."""

    code = "waterbody_model_error"
    retryable = False
    default_message = "The waterbody model request failed."

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation_id: str,
    ) -> None:
        super().__init__(
            self.default_message,
            provider=provider,
            operation=operation_id,
        )
        self.model = model


class WaterbodyModelUnavailableError(WaterbodyModelPortError, PortUnavailableError):
    code = "waterbody_model_unavailable"
    retryable = True
    default_message = "The waterbody model is unavailable."


class WaterbodyModelTimeoutError(WaterbodyModelPortError, PortTimeoutError):
    code = "waterbody_model_timeout"
    retryable = True
    default_message = "The waterbody model request timed out."


class WaterbodyModelContentFilteredError(WaterbodyModelPortError):
    code = "waterbody_model_content_filtered"
    default_message = "The waterbody model response was filtered."


class WaterbodyModelRefusalError(WaterbodyModelPortError):
    code = "waterbody_model_refused"
    default_message = "The waterbody model refused the request."


class WaterbodyModelProtocolError(WaterbodyModelPortError, InvalidPortResponseError):
    code = "waterbody_model_response_invalid"
    default_message = "The waterbody model returned an invalid response."


class WaterbodyModelTruncatedError(WaterbodyModelPortError):
    code = "waterbody_model_truncated"
    default_message = "The waterbody model response was truncated."


class WaterbodyModelIncompleteError(WaterbodyModelPortError):
    code = "waterbody_model_incomplete"
    default_message = "The waterbody model response was incomplete."


class WaterbodyModelCancelledError(WaterbodyModelPortError):
    code = "waterbody_model_cancelled"
    default_message = "The waterbody model request was cancelled."
