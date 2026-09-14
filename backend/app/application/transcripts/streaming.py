import re

from app.application.transcripts.redaction import (
    REDACTED,
    SENSITIVE_NAME_PATTERN,
    private_variants,
    redact_text,
)

_ASSIGNMENT_PREFIX = re.compile(
    rf"(?P<prefix>[\"']?{SENSITIVE_NAME_PATTERN}[\"']?\s*[:=]\s*(?P<quote>[\"']?))",
    re.IGNORECASE,
)
_HEADER_PREFIX = re.compile(
    r"(?P<prefix>\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)",
    re.IGNORECASE,
)
_AUTH_PREFIX = re.compile(r"\b(?:Bearer|Basic)\s+", re.IGNORECASE)
_URL_PREFIX = re.compile(r"https?://", re.IGNORECASE)
_TOKEN_DELIMITERS = frozenset("\r\n\t ,;}&]\"'")
_CANDIDATE_MARKERS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "api-key",
    "api key",
    "apikey",
    "access_key",
    "access-token",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "credential",
    "private_key",
    "bearer",
    "basic",
    "http://",
    "https://",
)


class StreamingCredentialFilter:
    """Incremental credential redaction with a bounded private look-behind buffer."""

    def __init__(self, private_values: tuple[str, ...] = ()) -> None:
        self._private_values = private_values
        self._variants = private_variants(private_values)
        self._holdback = max((48, *(len(value) for value in self._variants)))
        self._pending = ""
        self._mode = "normal"
        self._quote = ""
        self._escaped = False
        self._closed = False

    @property
    def buffered_characters(self) -> int:
        return len(self._pending)

    def feed(self, chunk: str) -> str:
        if self._closed:
            raise RuntimeError("streaming credential filter is already finished")
        if not isinstance(chunk, str):
            raise TypeError("streaming chunks must be text")
        self._pending += chunk
        return self._drain(final=False)

    def cancel(self) -> str:
        if self._closed:
            return ""
        output = self._drain(final=False)
        if self._mode == "normal" and self._pending:
            candidate_start = self._candidate_start()
            output += redact_text(
                self._pending[:candidate_start],
                self._private_values,
            )
            if candidate_start < len(self._pending):
                output += REDACTED
                self._mode = "token"
            self._pending = ""
        elif self._mode != "normal":
            self._pending = ""
        return output

    def finish(self) -> str:
        if self._closed:
            return ""
        output = self._drain(final=True)
        self._pending = ""
        self._closed = True
        return output

    def _drain(self, *, final: bool) -> str:
        output: list[str] = []
        while self._pending:
            if self._mode != "normal":
                released = self._consume_masked()
                output.append(released)
                if self._mode != "normal":
                    break
                continue

            url = _URL_PREFIX.search(self._pending)
            marker = self._first_marker()
            secret = self._first_known_secret()
            starts = [
                value for value in (
                    (url.start(), "url", url) if url else None,
                    marker,
                    secret,
                )
                if value is not None
            ]
            selected = min(starts, key=lambda value: (value[0], value[1])) if starts else None
            if selected is not None and selected[1] == "url":
                if self._consume_url(output, selected[2], final=final):
                    continue
                break
            if selected is not None and selected[1] == "known":
                start, _, value = selected
                output.append(self._pending[:start])
                output.append(REDACTED)
                self._pending = self._pending[start + len(value):]
                continue
            if selected is not None:
                start, kind, match = selected
                output.append(self._pending[:start])
                if kind == "auth":
                    output.append(REDACTED)
                    self._mode = "token"
                else:
                    output.append(match.group("prefix") + REDACTED)
                    if kind == "header":
                        self._mode = "header"
                    else:
                        self._quote = match.group("quote") or ""
                        self._mode = "quoted" if self._quote else "token"
                self._pending = self._pending[match.end():]
                continue

            if final:
                output.append(self._safe_final_pending())
                self._pending = ""
                break
            releasable = len(self._pending) - self._holdback
            if releasable <= 0:
                break
            output.append(self._pending[:releasable])
            self._pending = self._pending[releasable:]
        if final and self._mode != "normal":
            self._pending = ""
        return "".join(output)

    def _first_marker(self):
        matches = []
        for priority, (kind, pattern) in enumerate((
            ("header", _HEADER_PREFIX),
            ("assignment", _ASSIGNMENT_PREFIX),
            ("auth", _AUTH_PREFIX),
        )):
            match = pattern.search(self._pending)
            if match is not None:
                matches.append((match.start(), priority, kind, match))
        if not matches:
            return None
        start, _, kind, match = min(matches)
        return start, kind, match

    def _first_known_secret(self):
        found = [
            (self._pending.find(value), "known", value)
            for value in self._variants
            if value and value in self._pending
        ]
        return min(found, key=lambda value: value[0]) if found else None

    def _consume_masked(self) -> str:
        if self._mode == "header":
            positions = [
                position for delimiter in ("\r", "\n")
                if (position := self._pending.find(delimiter)) >= 0
            ]
            if not positions:
                self._pending = ""
                return ""
            position = min(positions)
            delimiter = self._pending[position]
            self._pending = self._pending[position + 1:]
            self._mode = "normal"
            return delimiter

        if self._mode == "quoted":
            for index, character in enumerate(self._pending):
                if self._escaped:
                    self._escaped = False
                    continue
                if character == "\\":
                    self._escaped = True
                    continue
                if character == self._quote:
                    self._pending = self._pending[index + 1:]
                    self._mode = "normal"
                    self._quote = ""
                    return character
            self._pending = ""
            return ""

        for index, character in enumerate(self._pending):
            if character in _TOKEN_DELIMITERS:
                self._pending = self._pending[index + 1:]
                self._mode = "normal"
                return character
        self._pending = ""
        return ""

    def _consume_url(self, output: list[str], match, *, final: bool) -> bool:
        start = match.start()
        end = next(
            (
                index
                for index in range(match.end(), len(self._pending))
                if self._pending[index].isspace()
            ),
            None,
        )
        if end is None and not final:
            output.append(self._pending[:start])
            self._pending = self._pending[start:]
            return False
        end = len(self._pending) if end is None else end
        candidate = self._pending[start:end]
        authority = candidate[match.group().find("://") + 3:].split("/", 1)[0]
        if ":" in authority and "@" not in authority and final:
            rendered = match.group() + REDACTED
        else:
            rendered = redact_text(candidate, self._private_values)
        output.append(self._pending[:start] + rendered)
        self._pending = self._pending[end:]
        return True

    def _safe_final_pending(self) -> str:
        candidate_start = self._candidate_start()
        safe = redact_text(self._pending[:candidate_start], self._private_values)
        if candidate_start < len(self._pending):
            safe += REDACTED
        return safe

    def _candidate_start(self) -> int:
        lowered = self._pending.lower()
        candidate_start = len(self._pending)
        for index in range(len(lowered)):
            suffix = lowered[index:]
            marker_candidate = len(suffix) >= 4 and any(
                marker.startswith(suffix) for marker in _CANDIDATE_MARKERS
            )
            private_candidate = len(suffix) >= 4 and any(
                value.lower().startswith(suffix) for value in self._variants
            )
            if marker_candidate or private_candidate:
                candidate_start = index
                break
        return candidate_start
