import json
import re

REDACTED = "[REDACTED]"
SENSITIVE_NAME_PATTERN = (
    r"(?:[\w-]*(?:authorization|cookie|api[ _-]?key|access[ _-]?key|token|secret|password|"
    r"passwd|credential|private[ _-]?key)[\w-]*|request[_-]?body|headers)"
)
SENSITIVE_KEY = re.compile(rf"^{SENSITIVE_NAME_PATTERN}$", re.IGNORECASE)
ASSIGNMENT = re.compile(
    rf"(?P<key>[\"']?{SENSITIVE_NAME_PATTERN}[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,}&]+)",
    re.IGNORECASE,
)
AUTH_SCHEME = re.compile(r"\b(?:Bearer|Basic)\s+[^\s,;\"'}]+", re.IGNORECASE)
HEADER_VALUE = re.compile(
    r"(?P<key>\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
URL_CREDENTIAL = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.IGNORECASE)


def private_variants(private_values: tuple[str, ...]) -> tuple[str, ...]:
    variants = {
        variant
        for private in private_values
        if private
        for variant in (
            private,
            repr(private)[1:-1],
            json.dumps(private)[1:-1],
            json.dumps(private, ensure_ascii=False)[1:-1],
        )
        if variant
    }
    return tuple(sorted(variants, key=len, reverse=True))


def redact_text(value: str, private_values: tuple[str, ...] = ()) -> str:
    for private in private_variants(private_values):
        value = value.replace(private, REDACTED)
    value = HEADER_VALUE.sub(lambda match: match["key"] + REDACTED, value)
    value = ASSIGNMENT.sub(lambda match: match["key"] + REDACTED, value)
    value = AUTH_SCHEME.sub(REDACTED, value)
    return URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)


def redact(value: object, private_values: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        return {
            redact_text(key, private_values): (
                REDACTED
                if SENSITIVE_KEY.fullmatch(key)
                else redact(item, private_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, private_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, private_values)
    if value is None or type(value) in (bool, int, float):
        return value
    raise TypeError("Expected a JSON-like value.")
