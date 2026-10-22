import json
import re
from functools import lru_cache
from threading import Lock

_REGISTERED_PRIVATE_VALUES: set[str] = set()
_REGISTERED_PRIVATE_VALUES_LOCK = Lock()


def register_private_value(value: str) -> None:
    if value:
        with _REGISTERED_PRIVATE_VALUES_LOCK:
            _REGISTERED_PRIVATE_VALUES.add(value)


def registered_private_values() -> tuple[str, ...]:
    with _REGISTERED_PRIVATE_VALUES_LOCK:
        return tuple(_REGISTERED_PRIVATE_VALUES)

REDACTED = "[REDACTED]"
SENSITIVE_NAME_PATTERN = (
    r"(?:[\w-]*(?:authorization|cookie|api[ _-]?key|access[ _-]?key|token|secret|password|"
    r"passwd|credential|private[ _-]?key)[\w-]*|request[_-]?body|headers)"
)
SENSITIVE_KEY = re.compile(rf"^{SENSITIVE_NAME_PATTERN}$", re.IGNORECASE)
_SAFE_NUMERIC_METRIC_KEYS = frozenset({"input_tokens", "output_tokens"})
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
_AUTH_MARKERS = ("bearer", "basic")
_URL_MARKERS = ("https://", "http://")
_SENSITIVE_MARKERS = (
    "proxy-authorization",
    "authorization",
    "request_body",
    "request-body",
    "requestbody",
    "private_key",
    "private-key",
    "private key",
    "privatekey",
    "access_key",
    "access-key",
    "access key",
    "accesskey",
    "api_key",
    "api-key",
    "api key",
    "apikey",
    "set-cookie",
    "cookie",
    "password",
    "credential",
    "headers",
    "passwd",
    "secret",
    "token",
)
_SYNTAX_MARKERS = tuple(
    sorted(
        (
            *((marker, "url") for marker in _URL_MARKERS),
            *((marker, "auth") for marker in _AUTH_MARKERS),
            *((marker, "key") for marker in _SENSITIVE_MARKERS),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    ),
)
_SYNTAX_MARKER_KIND = {marker: kind for marker, kind in _SYNTAX_MARKERS}
_KEY_PREFIXES = frozenset(marker[:length] for marker, kind in _SYNTAX_MARKERS
                          if kind == "key" for length in range(1, len(marker) + 1))
_AUTH_PREFIXES = frozenset(marker[:length] for marker, kind in _SYNTAX_MARKERS
                           if kind == "auth" for length in range(1, len(marker) + 1))
_NON_AUTH_PREFIXES = _KEY_PREFIXES | frozenset(marker[:length] for marker, kind in _SYNTAX_MARKERS
                                             if kind == "url" for length in range(1, len(marker) + 1))
_SYNTAX_PATTERN = re.compile(
    "|".join(re.escape(marker) for marker, _ in _SYNTAX_MARKERS),
    re.IGNORECASE | re.ASCII,
)
_NORMAL_HOLDBACK = max(len(marker) for marker, _ in _SYNTAX_MARKERS)
_TOKEN_DELIMITERS = frozenset("\r\n\t ,;}&]\"'")
_URL_AUTHORITY_DELIMITERS = frozenset("/?#")
_HEADER_NAMES = frozenset(
    ("authorization", "proxy-authorization", "cookie", "set-cookie")
)
_MAX_URL_AUTHORITY_CHARACTERS = 2048
_MAX_SYNTAX_CANDIDATE_CHARACTERS = 512
_SCAN_BLOCK_CHARACTERS = 4096


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
    redactor = _CredentialRedactor(private_values)
    return redactor.feed(value) + redactor.finish()


def _ascii_lower(value: str) -> str:
    return value.translate(_ASCII_LOWER)


def _is_word_character(value: str) -> bool:
    return value == "_" or value.isalnum()


def _erases_masking_prefix(value: str) -> bool:
    lowered = _ascii_lower(value)
    without_space = lowered.rstrip()
    if len(without_space) < len(lowered):
        for marker in _AUTH_MARKERS:
            if without_space.endswith(marker):
                start = len(without_space) - len(marker)
                if start == 0 or not _is_word_character(without_space[start - 1]):
                    return True

    candidate = without_space
    if candidate.endswith(("\"", "'")):
        candidate = candidate[:-1].rstrip()
    if not candidate.endswith((":", "=")):
        return False
    key = candidate[:-1].rstrip().strip("\"'")
    return any(marker in key for marker in _SENSITIVE_MARKERS)


class _KnownValueFilter:
    def __init__(self, variants: tuple[str, ...]) -> None:
        self._variants = variants
        self._pattern = (
            re.compile("|".join(re.escape(value) for value in variants))
            if variants
            else None
        )
        self._prefix_tables = tuple(self._prefix_table(value) for value in variants)
        self._pending = ""
        self._suppressed = 0

    @property
    def buffered_characters(self) -> int:
        return len(self._pending)

    def feed(self, chunk: str) -> str:
        if not self._variants:
            return chunk
        self._pending += chunk
        return self._drain()

    def cancel(self) -> str:
        if not self._variants:
            return ""
        output = self._drain()
        if len(self._pending) > self._suppressed:
            output += REDACTED
            self._suppressed = len(self._pending)
        return output

    def finish(self) -> str:
        if not self._variants:
            return ""
        output = self._drain()
        if self._pending:
            visible = self._pending[self._suppressed:]
            if len(visible) >= 4:
                output += REDACTED
            else:
                output += visible
            self._pending = ""
            self._suppressed = 0
        return output

    @staticmethod
    def _prefix_table(pattern: str) -> tuple[int, ...]:
        table = [0] * len(pattern)
        matched = 0
        for index in range(1, len(pattern)):
            while matched and pattern[index] != pattern[matched]:
                matched = table[matched - 1]
            if pattern[index] == pattern[matched]:
                matched += 1
                table[index] = matched
        return tuple(table)

    @staticmethod
    def _suffix_prefix_length(
        value: str,
        pattern: str,
        table: tuple[int, ...],
    ) -> int:
        matched = 0
        for character in value:
            while matched and character != pattern[matched]:
                matched = table[matched - 1]
            if character == pattern[matched]:
                matched += 1
            if matched == len(pattern):
                matched = table[matched - 1]
        return matched

    def _holdback(self) -> int:
        if any(self._pending.endswith(value) for value in self._variants):
            return 0
        return max(
            (
                self._suffix_prefix_length(self._pending, value, table)
                for value, table in zip(self._variants, self._prefix_tables)
            ),
            default=0,
        )

    def _take_raw(self, length: int) -> str:
        value = self._pending[:length]
        self._pending = self._pending[length:]
        hidden = min(length, self._suppressed)
        self._suppressed -= hidden
        return value[hidden:]

    def _discard_match(self, length: int) -> str:
        hidden = min(length, self._suppressed)
        self._pending = self._pending[length:]
        self._suppressed -= hidden
        return "" if hidden else REDACTED

    def _drain(self) -> str:
        if not self._pending:
            return ""
        holdback = self._holdback()
        releasable = len(self._pending) - holdback
        if releasable <= 0:
            return ""

        output: list[str] = []
        segment = self._pending[:releasable]
        cursor = 0
        assert self._pattern is not None
        for match in self._pattern.finditer(segment):
            output.append(self._take_raw(match.start() - cursor))
            output.append(self._discard_match(match.end() - match.start()))
            cursor = match.end()
        output.append(self._take_raw(releasable - cursor))

        return "".join(output)


class _SyntaxFilter:
    def __init__(self) -> None:
        self._pending = ""
        self._suppressed = 0
        self._mode = "normal"
        self._marker_length = 0
        self._candidate_boundary = False
        self._quote = ""
        self._escaped = False
        self._masked_prefix: str | None = ""
        self._url_prefix = ""
        self._url_authority = ""
        self._url_output_emitted = False
        self._previous_raw = ""

    @property
    def buffered_characters(self) -> int:
        return len(self._pending) + len(self._url_authority)

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        return self._drain(final=False)

    def cancel(self) -> str:
        output = self._drain(final=False)
        if self._mode == "key" and self._key_separator_index() is not None:
            output += self._consume_key_candidate(final=True)
        elif self._mode == "normal" and self._pending:
            partial = self._partial_start()
            output += self._take_raw(partial)
            if len(self._pending) > self._suppressed:
                visible = self._pending[self._suppressed:]
                if len(visible) >= 3 or self._boundary_before(self._suppressed):
                    output += REDACTED
                else:
                    output += visible
                self._suppressed = len(self._pending)
        elif self._mode in {"key", "auth"} and self._pending:
            if len(self._pending) > self._suppressed:
                output += REDACTED
                self._suppressed = len(self._pending)
        elif self._mode == "url_authority":
            if not self._url_output_emitted:
                output += self._url_prefix + REDACTED
                self._url_output_emitted = True
            self._url_authority = ""
            self._mode = "url_masked"
        return output

    def finish(self) -> str:
        output = self._drain(final=True)
        self._pending = ""
        self._suppressed = 0
        return output

    def _take_raw(self, length: int) -> str:
        value = self._pending[:length]
        self._pending = self._pending[length:]
        hidden = min(length, self._suppressed)
        self._suppressed -= hidden
        if value:
            self._previous_raw = value[-1]
        return value[hidden:]

    def _discard(self, length: int) -> None:
        value = self._pending[:length]
        self._pending = self._pending[length:]
        hidden = min(length, self._suppressed)
        self._suppressed -= hidden
        if value:
            self._previous_raw = value[-1]

    def _boundary_before(self, start: int) -> bool:
        previous = self._pending[start - 1] if start else self._previous_raw
        return not previous or not _is_word_character(previous)

    def _find_marker(self) -> tuple[int, str, str] | None:
        match = _SYNTAX_PATTERN.search(self._pending)
        if match is None:
            return None
        marker = _ascii_lower(match.group())
        return match.start(), _SYNTAX_MARKER_KIND[marker], marker

    def _partial_start(self) -> int:
        lowered = _ascii_lower(self._pending)
        earliest = len(self._pending)
        for start in range(max(0, len(lowered) - _NORMAL_HOLDBACK + 1), len(lowered)):
            suffix = lowered[start:]
            if suffix in _NON_AUTH_PREFIXES or (suffix in _AUTH_PREFIXES and self._boundary_before(start)):
                earliest = start
                break
        return earliest

    def _mask_partial_at_finish(self) -> bool:
        visible = self._pending[self._suppressed:]
        if len(visible) < 3 or not self._boundary_before(self._suppressed):
            return False
        lowered = _ascii_lower(visible)
        return lowered in _KEY_PREFIXES

    def _drain(self, *, final: bool) -> str:
        output: list[str] = []
        while self._pending or (final and self._mode.startswith("url_")):
            if self._mode == "normal":
                marker = self._find_marker()
                if marker is None:
                    if not final:
                        releasable = max(0, len(self._pending) - _NORMAL_HOLDBACK)
                        output.append(self._take_raw(releasable))
                        break
                    partial = self._partial_start()
                    output.append(self._take_raw(partial))
                    if final and self._pending:
                        if self._mask_partial_at_finish():
                            output.append(REDACTED)
                            self._discard(len(self._pending))
                        else:
                            output.append(self._take_raw(len(self._pending)))
                    break
                start, kind, literal = marker
                output.append(self._take_raw(start))
                if kind == "url":
                    was_suppressed = self._suppressed > 0
                    self._url_prefix = self._take_raw(len(literal))
                    self._url_authority = ""
                    self._url_output_emitted = was_suppressed
                    self._mode = "url_masked" if was_suppressed else "url_authority"
                elif kind == "auth":
                    self._marker_length = len(literal)
                    self._candidate_boundary = self._boundary_before(0)
                    self._mode = "auth"
                else:
                    self._marker_length = len(literal)
                    self._candidate_boundary = self._boundary_before(0)
                    self._mode = "key"
                continue
            previous_mode = self._mode
            previous_pending = len(self._pending)
            if self._mode == "key":
                released = self._consume_key_candidate(final=final)
            elif self._mode == "auth":
                released = self._consume_auth_candidate(final=final)
            elif self._mode.startswith("url_"):
                released = self._consume_url(final=final)
            elif self._mode == "header":
                released = self._consume_header()
            elif self._mode == "quoted":
                released = self._consume_quoted()
            elif self._mode == "token":
                released = self._consume_token()
            else:
                released = self._consume_overflow(final=final)
            output.append(released)
            if (
                not released
                and self._mode == previous_mode
                and len(self._pending) == previous_pending
            ):
                break
            if self._mode != "normal" and not self._pending:
                break
        return "".join(output)

    def _consume_key_candidate(self, *, final: bool) -> str:
        index = self._marker_length
        while index < len(self._pending) and (
            _is_word_character(self._pending[index]) or self._pending[index] == "-"
        ):
            index += 1
        if index == len(self._pending) and not final:
            return self._overflow_candidate() if index > _MAX_SYNTAX_CANDIDATE_CHARACTERS else ""

        if index < len(self._pending) and self._pending[index] in "\"'":
            index += 1
        while index < len(self._pending) and self._pending[index].isspace():
            index += 1
        if index == len(self._pending) and not final:
            return self._overflow_candidate() if index > _MAX_SYNTAX_CANDIDATE_CHARACTERS else ""
        if index == len(self._pending) or self._pending[index] not in ":=":
            released = self._take_raw(index)
            self._mode = "normal"
            return released

        separator = self._pending[index]
        index += 1
        while index < len(self._pending) and self._pending[index].isspace():
            index += 1
        if index == len(self._pending) and not final:
            return self._overflow_candidate() if index > _MAX_SYNTAX_CANDIDATE_CHARACTERS else ""

        quote = self._pending[index] if index < len(self._pending) and self._pending[index] in "\"'" else ""
        if quote:
            index += 1
        raw_prefix = self._pending[:index]
        key = _ascii_lower(raw_prefix[: raw_prefix.find(separator)]).strip("\"' \t\r\n")
        already_redacted = self._pending.startswith(REDACTED, index)
        redacted_end = index + len(REDACTED)
        if already_redacted:
            following = self._pending[redacted_end:redacted_end + 1]
            if quote:
                already_redacted = following == quote
            elif separator == ":" and key in _HEADER_NAMES and self._candidate_boundary:
                already_redacted = not following or following in "\r\n"
            else:
                already_redacted = not following or following in _TOKEN_DELIMITERS
        visible_prefix = self._take_raw(index)
        self._quote = quote
        self._escaped = False
        if separator == ":" and key in _HEADER_NAMES and self._candidate_boundary:
            self._mode = "header"
        else:
            self._mode = "quoted" if quote else "token"
            self._masked_prefix = ""
        if already_redacted:
            self._discard(len(REDACTED))
            if not quote:
                self._mode = "normal"
        return visible_prefix + REDACTED

    def _key_separator_index(self) -> int | None:
        index = self._marker_length
        while index < len(self._pending) and (
            _is_word_character(self._pending[index]) or self._pending[index] == "-"
        ):
            index += 1
        if index < len(self._pending) and self._pending[index] in "\"'":
            index += 1
        while index < len(self._pending) and self._pending[index].isspace():
            index += 1
        if index < len(self._pending) and self._pending[index] in ":=":
            return index
        return None

    def _overflow_candidate(self) -> str:
        was_suppressed = self._suppressed > 0
        self._discard(len(self._pending))
        self._mode = "overflow"
        return "" if was_suppressed else REDACTED

    def _consume_auth_candidate(self, *, final: bool) -> str:
        if len(self._pending) == self._marker_length and not final:
            return ""
        if (
            not self._candidate_boundary
            or len(self._pending) == self._marker_length
            or not self._pending[self._marker_length].isspace()
        ):
            released = self._take_raw(self._marker_length)
            self._mode = "normal"
            return released

        index = self._marker_length
        while index < len(self._pending) and self._pending[index].isspace():
            index += 1
        was_suppressed = self._suppressed > 0
        self._discard(index)
        self._mode = "token"
        self._masked_prefix = ""
        return "" if was_suppressed else REDACTED

    def _consume_url(self, *, final: bool) -> str:
        delimiter_index = next(
            (
                index
                for index, character in enumerate(self._pending)
                if character.isspace() or character in _URL_AUTHORITY_DELIMITERS
            ),
            None,
        )
        authority_length = len(self._pending) if delimiter_index is None else delimiter_index
        authority = self._pending[:authority_length]
        self._discard(authority_length)
        output: list[str] = []

        if self._mode == "url_authority":
            available = _MAX_URL_AUTHORITY_CHARACTERS - len(self._url_authority)
            self._url_authority += authority[:available]
            if len(authority) > available:
                output.append(self._url_prefix + REDACTED)
                self._url_authority = ""
                self._url_output_emitted = True
                self._mode = "url_masked"

        if delimiter_index is not None or final:
            if self._mode == "url_authority" and not self._url_output_emitted:
                if "@" in self._url_authority:
                    _, _, host = self._url_authority.rpartition("@")
                    output.append(self._url_prefix + REDACTED + "@" + host)
                else:
                    output.append(self._url_prefix + self._url_authority)
            self._url_prefix = ""
            self._url_authority = ""
            self._url_output_emitted = False
            self._mode = "normal"
            if delimiter_index is not None:
                output.append(self._take_raw(1))
        return "".join(output)

    def _consume_header(self) -> str:
        positions = [
            position
            for delimiter in ("\r", "\n")
            if (position := self._pending.find(delimiter)) >= 0
        ]
        if not positions:
            self._discard(len(self._pending))
            return ""
        position = min(positions)
        self._discard(position)
        delimiter = self._take_raw(1)
        self._mode = "normal"
        return delimiter

    def _consume_quoted(self) -> str:
        for index, character in enumerate(self._pending):
            if self._escaped:
                self._escaped = False
                continue
            if character == "\\":
                self._escaped = True
                continue
            if character == self._quote:
                self._discard(index)
                quote = self._take_raw(1)
                self._quote = ""
                self._mode = "normal"
                return quote
        self._discard(len(self._pending))
        return ""

    def _consume_token(self) -> str:
        for index, character in enumerate(self._pending):
            if character in _TOKEN_DELIMITERS:
                self._extend_masked_prefix(self._pending[:index])
                self._discard(index)
                delimiter = self._take_raw(1)
                suppress_delimiter = (
                    delimiter == "]" and self._masked_prefix == REDACTED[:-1]
                )
                self._masked_prefix = ""
                self._mode = "normal"
                return "" if suppress_delimiter else delimiter
        self._extend_masked_prefix(self._pending)
        self._discard(len(self._pending))
        return ""

    def _extend_masked_prefix(self, value: str) -> None:
        if self._masked_prefix is None:
            return
        candidate = self._masked_prefix + value
        self._masked_prefix = candidate if REDACTED.startswith(candidate) else None

    def _consume_overflow(self, *, final: bool) -> str:
        for index, character in enumerate(self._pending):
            if character in "\r\n,;&}":
                self._discard(index)
                delimiter = self._take_raw(1)
                self._mode = "normal"
                return delimiter
        self._discard(len(self._pending))
        if final:
            self._mode = "normal"
        return ""


class _CredentialRedactor:
    def __init__(self, private_values: tuple[str, ...] = ()) -> None:
        variants = private_variants(private_values)
        pre_syntax_variants = tuple(
            value for value in variants if not _erases_masking_prefix(value)
        )
        self._pre_known = _KnownValueFilter(pre_syntax_variants)
        self._syntax = _SyntaxFilter()
        self._post_known = _KnownValueFilter(variants)
        self._closed = False

    @property
    def buffered_characters(self) -> int:
        return (
            self._pre_known.buffered_characters
            + self._syntax.buffered_characters
            + self._post_known.buffered_characters
        )

    def feed(self, chunk: str) -> str:
        if self._closed:
            raise RuntimeError("streaming credential filter is already finished")
        if not isinstance(chunk, str):
            raise TypeError("streaming chunks must be text")
        output: list[str] = []
        for start in range(0, len(chunk), _SCAN_BLOCK_CHARACTERS):
            block = chunk[start:start + _SCAN_BLOCK_CHARACTERS]
            safe = self._pre_known.feed(block)
            safe = self._syntax.feed(safe)
            output.append(self._post_known.feed(safe))
        return "".join(output)

    def cancel(self) -> str:
        if self._closed:
            return ""
        syntax_tail = self._syntax.feed(self._pre_known.cancel())
        syntax_tail += self._syntax.cancel()
        output = self._post_known.cancel()
        output += self._post_known.feed(syntax_tail)
        output += self._post_known.cancel()
        return output

    def finish(self) -> str:
        if self._closed:
            return ""
        syntax_tail = self._syntax.feed(self._pre_known.finish())
        syntax_tail += self._syntax.finish()
        output = self._post_known.feed(syntax_tail)
        output += self._post_known.finish()
        self._closed = True
        return output


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
    @lru_cache(maxsize=256)
    def safe_text(text: str) -> str:
        return redact_text(text, private_values)

    def visit(item: object) -> object:
        if isinstance(item, dict):
            return {
                safe_text(key): (
                    REDACTED
                    if (
                        SENSITIVE_KEY.fullmatch(key)
                        and not (
                            key.lower() in _SAFE_NUMERIC_METRIC_KEYS
                            and (child is None or type(child) is int)
                        )
                    )
                    else visit(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return safe_text(item)
        if item is None or type(item) in (bool, int, float):
            return item
        raise TypeError("Expected a JSON-like value.")

    return visit(value)
