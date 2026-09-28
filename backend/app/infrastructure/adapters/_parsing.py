from collections.abc import Mapping

from app.ports import InvalidPortResponseError


def require_mapping(
    value: object,
    *,
    provider: str,
    operation: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise invalid_response(provider, operation)
    return value


def require_list(
    value: object,
    *,
    provider: str,
    operation: str,
) -> list[object]:
    if not isinstance(value, list):
        raise invalid_response(provider, operation)
    return value


def require_string(
    value: object,
    *,
    provider: str,
    operation: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid_response(provider, operation)
    return value.strip()


def require_number(
    value: object,
    *,
    provider: str,
    operation: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_response(provider, operation)
    return float(value)


def invalid_response(
    provider: str,
    operation: str,
) -> InvalidPortResponseError:
    return InvalidPortResponseError(
        "Provider response did not match the expected schema.",
        provider=provider,
        operation=operation,
    )
