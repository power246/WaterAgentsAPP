import json
import re

REDACTED = "[REDACTED]"
_SENSITIVE_NAME = (
    r"(?:[\w-]*(?:authorization|cookie|api[ _-]?key|access[ _-]?key|token|secret|password|"
    r"passwd|credential|private[ _-]?key)[\w-]*|query|request[_-]?body|headers)"
)
_SENSITIVE_KEY = re.compile(rf"^{_SENSITIVE_NAME}$", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    rf"(?P<key>[\"']?{_SENSITIVE_NAME}[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,}&]+)",
    re.IGNORECASE,
)
_AUTH_SCHEME = re.compile(r"\b(?:Bearer|Basic)\s+[^\s,;\"'}]+", re.IGNORECASE)
_HEADER_VALUE = re.compile(
    r"(?P<key>\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
_URL_CREDENTIAL = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.IGNORECASE)


def redact_text(value: str, private_values: tuple[str, ...] = ()) -> str:
    variants = {
        variant for private in private_values if private
        for variant in (private, repr(private)[1:-1], json.dumps(private)[1:-1],
                        json.dumps(private, ensure_ascii=False)[1:-1])
    }
    for private in sorted(variants, key=len, reverse=True):
        value = value.replace(private, REDACTED)
    value = _HEADER_VALUE.sub(lambda match: match["key"] + REDACTED, value)
    value = _ASSIGNMENT.sub(lambda match: match["key"] + REDACTED, value)
    value = _AUTH_SCHEME.sub(REDACTED, value)
    return _URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            redact_text(key): REDACTED if _SENSITIVE_KEY.fullmatch(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or type(value) in (bool, int, float):
        return value
    raise TypeError("Expected a JSON-like value.")
