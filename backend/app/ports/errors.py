class PortError(Exception):
    """Base error for failures while using an external capability."""


class PortUnavailableError(PortError):
    """Raised when an external capability cannot serve the request."""


class PortTimeoutError(PortError):
    """Raised when an external capability does not respond in time."""


class InvalidPortResponseError(PortError):
    """Raised when an external response violates the port contract."""
