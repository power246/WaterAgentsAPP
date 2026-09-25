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
