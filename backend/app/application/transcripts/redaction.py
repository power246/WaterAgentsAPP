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
AUTH_CREDENTIAL = re.compile(
    r"\b(?:Bearer|Basic)\s+[^\s,;\"'}]+",
    re.IGNORECASE,
)
HEADER_VALUE = re.compile(
    r"(?P<key>\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
URL_CREDENTIAL = re.compile(r"(https?://)[^\s/@]+@", re.IGNORECASE)
_SENSITIVE_CANDIDATES = (
    "authorization",
    "cookie",
    "api",
    "access",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "private",
    "request",
    "headers",
)


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
    lowered = value.casefold()
    if any(candidate in lowered for candidate in _SENSITIVE_CANDIDATES):
        value = HEADER_VALUE.sub(lambda match: match["key"] + REDACTED, value)
        value = ASSIGNMENT.sub(lambda match: match["key"] + REDACTED, value)
    if "bearer" in lowered or "basic" in lowered:
        value = AUTH_CREDENTIAL.sub(REDACTED, value)
    if "http://" in lowered or "https://" in lowered:
        value = URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)
    return value


def redact_tags(
    tags: tuple[str, ...],
    private_values: tuple[str, ...] = (),
) -> tuple[str, ...]:
    safe_tags: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        safe_tag = redact_text(tag, private_values)
        if safe_tag not in seen:
            seen.add(safe_tag)
            safe_tags.append(safe_tag)
    return tuple(safe_tags)


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
