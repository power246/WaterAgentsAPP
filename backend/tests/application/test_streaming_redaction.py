import json
import subprocess
import sys
import textwrap

import pytest

from app.application.transcripts.redaction import redact_text
from app.application.transcripts.streaming import StreamingCredentialFilter


def _render_at_every_split(text: str, private_values: tuple[str, ...] = ()) -> tuple[str, ...]:
    rendered = []
    for split in range(len(text) + 1):
        filter_ = StreamingCredentialFilter(private_values)
        rendered.append(filter_.feed(text[:split]) + filter_.feed(text[split:]) + filter_.finish())
    return tuple(rendered)


@pytest.mark.parametrize(
    ("text", "private_values", "secrets"),
    [
        ('before api_key =   "秘密🔑value" after', (), ("秘密🔑value",)),
        (
            "before Authorization: Bearer synthetic-token\nafter",
            (),
            ("synthetic-token",),
        ),
        (
            "before Cookie: sid=synthetic-cookie; HttpOnly\nafter",
            (),
            ("synthetic-cookie",),
        ),
        (
            "before https://user:synthetic-password@provider.test/path after",
            (),
            ("synthetic-password",),
        ),
        (
            "before client_secret='quoted\\\"value' after",
            (),
            ('quoted\\"value',),
        ),
        (
            "before explicit synthetic-private-value after",
            ("synthetic-private-value",),
            ("synthetic-private-value",),
        ),
    ],
)
def test_credentials_are_hidden_at_every_chunk_split(
    text: str,
    private_values: tuple[str, ...],
    secrets: tuple[str, ...],
) -> None:
    for rendered in _render_at_every_split(text, private_values):
        assert "[REDACTED]" in rendered
        assert all(secret not in rendered for secret in secrets)
        assert "before" in rendered
        assert "after" in rendered


def test_known_value_covers_repr_and_json_escape_forms_at_every_split() -> None:
    secret = '私密\n"credential"'
    forms = (secret, repr(secret), json.dumps(secret), json.dumps(secret, ensure_ascii=False))

    for form in forms:
        for rendered in _render_at_every_split("failure: " + form, (secret,)):
            assert "credential" not in rendered
            assert "[REDACTED]" in rendered


def test_cancel_keeps_masking_state_across_the_boundary() -> None:
    text = "api_key=synthetic-secret trailing safe"
    for split in range(len(text) + 1):
        filter_ = StreamingCredentialFilter(("synthetic-secret",))
        rendered = filter_.feed(text[:split]) + filter_.cancel()
        rendered += filter_.feed(text[split:]) + filter_.finish()
        assert "synthetic-secret" not in rendered


@pytest.mark.parametrize(
    ("prefix", "suffix", "secret"),
    (
        ("api_key=", "abcdef", "abcdef"),
        ('api_key="', 'abcdef" after', "abcdef"),
        ("Authorization: ", "abcdef\nafter", "abcdef"),
        ("Bearer ", "abcdefgh after", "abcdefgh"),
        ("Basic ", "dXNlcjpwYXNz after", "dXNlcjpwYXNz"),
        ("https://user:", "abcdef@example.test/path", "abcdef"),
    ),
)
def test_cancel_preserves_syntax_masking_without_private_values(
    prefix: str,
    suffix: str,
    secret: str,
) -> None:
    filter_ = StreamingCredentialFilter()

    original = filter_.feed(prefix) + filter_.cancel()
    late = filter_.feed(suffix) + filter_.finish()

    assert secret not in original
    assert secret not in late
    assert secret not in original + late
    assert "[REDACTED]" in original


@pytest.mark.parametrize(
    ("prefix", "suffix", "secret"),
    (
        ("api_key=", "abcdef trailing safe", "abcdef"),
        ("https://user:", "abcdef@example.test/path", "abcdef"),
    ),
)
def test_cancelled_credentials_are_hidden_at_every_chunk_split(
    prefix: str,
    suffix: str,
    secret: str,
) -> None:
    for prefix_split in range(len(prefix) + 1):
        for suffix_split in range(len(suffix) + 1):
            filter_ = StreamingCredentialFilter()
            original = filter_.feed(prefix[:prefix_split])
            original += filter_.feed(prefix[prefix_split:]) + filter_.cancel()
            late = filter_.feed(suffix[:suffix_split])
            late += filter_.feed(suffix[suffix_split:]) + filter_.finish()

            assert secret not in original
            assert secret not in late
            assert secret not in original + late
            assert "[REDACTED]" in original


@pytest.mark.parametrize(
    ("text", "secret"),
    (
        ("api_key=alphabetic trailing", "alphabetic"),
        ("password=alphabetic trailing", "alphabetic"),
        ("Bearer alphabetic trailing", "alphabetic"),
        ("https://user:alphabetic@example.test/path", "alphabetic"),
    ),
)
def test_every_syntax_cancel_position_keeps_original_and_late_text_safe(
    text: str,
    secret: str,
) -> None:
    for cancel_at in range(len(text) + 1):
        filter_ = StreamingCredentialFilter()
        original = filter_.feed(text[:cancel_at]) + filter_.cancel()
        late = filter_.feed(text[cancel_at:]) + filter_.finish()

        assert secret not in original
        assert secret not in late
        assert secret not in original + late
        assert filter_.cancel() == ""
        assert filter_.finish() == ""


@pytest.mark.parametrize(
    "text",
    (
        "https://user:alphabetic@example.test:8443/path?count=3",
        "https://token@example.test/path#fragment",
        "https://example.com:8443/path",
        "http://127.0.0.1:8080/path?api_key=alphabetic&count=3",
        "https://[2001:db8::1]:8443/path#fragment",
        "https://example.com:8443",
        "https://user:alphabetic@example.test",
    ),
)
def test_urls_match_static_redaction_at_every_chunk_split(text: str) -> None:
    expected = redact_text(text)
    if "@" in text:
        userinfo = text.split("://", 1)[1].rsplit("@", 1)[0]
        assert userinfo not in expected
        assert "[REDACTED]@" in expected

    assert all(rendered == expected for rendered in _render_at_every_split(text))


def test_long_url_path_has_a_fixed_internal_buffer_bound() -> None:
    text = "https://example.com/" + "a" * (128 * 1024)
    filter_ = StreamingCredentialFilter()
    rendered: list[str] = []

    for character in text:
        rendered.append(filter_.feed(character))
        assert filter_.buffered_characters <= 2048

    assert "".join(rendered) + filter_.finish() == text


def test_oversized_url_authority_is_bounded_and_conservatively_redacted() -> None:
    authority = "a" * (128 * 1024)
    filter_ = StreamingCredentialFilter()
    rendered: list[str] = []

    for character in "https://" + authority:
        rendered.append(filter_.feed(character))
        assert filter_.buffered_characters <= 2048

    assert "".join(rendered) + filter_.finish() == "https://[REDACTED]"


def test_normal_query_streams_incrementally_and_round_trips_unchanged() -> None:
    text = "Lake Erie water level and flow observations remain ordinary query text. " * 4
    filter_ = StreamingCredentialFilter()
    outputs = [filter_.feed(text[index:index + 9]) for index in range(0, len(text), 9)]

    assert any(outputs[:-1])
    assert "".join(outputs) + filter_.finish() == text


def test_finish_masks_an_unresolved_sensitive_candidate() -> None:
    filter_ = StreamingCredentialFilter()

    rendered = filter_.feed("safe api_ke") + filter_.finish()
    assert rendered == "safe [REDACTED]"


@pytest.mark.parametrize(
    ("prefix", "suffix", "private_values", "secrets"),
    (
        ("api", "_key=alphabetic ", (), ("alphabetic",)),
        ("Bea", "rer alphabetic ", (), ("alphabetic",)),
        ("pas", "sword=alphabetic ", (), ("alphabetic",)),
        ("s", "ynthetic-private ", ("synthetic-private",), ("synthetic-private",)),
        ("sy", "nthetic-private ", ("synthetic-private",), ("synthetic-private",)),
        ("syn", "thetic-private ", ("synthetic-private",), ("synthetic-private",)),
    ),
)
def test_cancel_masks_one_to_three_character_sensitive_prefixes(
    prefix: str,
    suffix: str,
    private_values: tuple[str, ...],
    secrets: tuple[str, ...],
) -> None:
    filter_ = StreamingCredentialFilter(private_values)

    original = filter_.feed(prefix) + filter_.cancel()
    late = filter_.feed(suffix) + filter_.finish()

    assert original == "[REDACTED]"
    assert all(secret not in original for secret in secrets)
    assert all(secret not in late for secret in secrets)
    assert all(secret not in original + late for secret in secrets)
    assert filter_.cancel() == ""
    assert filter_.finish() == ""


@pytest.mark.parametrize(
    "private_url",
    (
        "https://private.example/path",
        "https://private.example/path?query=value#fragment",
        "https://user:password@private.example/path",
        "https://private.example/水位?note=quoted%20value#片段",
    ),
)
def test_complete_private_url_is_hidden_at_every_chunk_split(private_url: str) -> None:
    text = f"before {private_url} after"

    for rendered in _render_at_every_split(text, (private_url,)):
        assert rendered == "before [REDACTED] after"
        assert private_url not in rendered


def test_overlapping_private_values_are_hidden_at_every_chunk_split() -> None:
    text = "before synthetic-private after private"
    for rendered in _render_at_every_split(
        text,
        ("synthetic-private", "private"),
    ):
        assert rendered == "before [REDACTED] after [REDACTED]"


@pytest.mark.parametrize("prefix", ("s", "sy", "syn"))
def test_finish_preserves_short_known_secret_prefix(prefix: str) -> None:
    filter_ = StreamingCredentialFilter(("synthetic-private",))
    assert filter_.feed(prefix) + filter_.finish() == prefix


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("api_key=alphabetic trailing safe", "api_key=[REDACTED] trailing safe"),
        ('api_key="alphabetic value" trailing safe', 'api_key="[REDACTED]" trailing safe'),
        ("api_key=[REDACTED]", "api_key=[REDACTED]"),
        ("query=Lake Erie water level", "query=Lake Erie water level"),
    ),
)
def test_assignment_boundaries_match_static_at_every_chunk_split(
    text: str,
    expected: str,
) -> None:
    assert redact_text(text) == expected
    assert all(rendered == expected for rendered in _render_at_every_split(text))


@pytest.mark.parametrize(
    ("private_prefix", "suffix"),
    (
        ("api_key=", "alphabetic trailing"),
        ('client_secret="', 'alphabetic" trailing'),
        ("Authorization: ", "alphabetic\ntrailing"),
        ("Bearer ", "alphabetic trailing"),
    ),
)
def test_private_value_replacement_cannot_expose_following_credential(
    private_prefix: str,
    suffix: str,
) -> None:
    text = private_prefix + suffix
    for rendered in _render_at_every_split(text, (private_prefix,)):
        assert private_prefix not in rendered
        assert "alphabetic" not in rendered
        assert "trailing" in rendered


@pytest.mark.parametrize(
    "text",
    (
        "https://example.com?contact=alice@example.net",
        "https://example.com/path#contact=alice@example.net",
        "https://example.com:8443/path?contact=alice@example.net",
        "https://[2001:db8::1]:8443/path#contact=alice@example.net",
    ),
)
def test_query_and_fragment_at_signs_are_not_url_credentials(text: str) -> None:
    assert redact_text(text) == text
    assert all(rendered == text for rendered in _render_at_every_split(text))


def test_large_single_delta_scales_in_an_isolated_process() -> None:
    script = textwrap.dedent(
        """
        import json
        import time

        from app.application.transcripts.streaming import StreamingCredentialFilter

        measurements = []
        for size in (4, 8, 16, 128, 512):
            repeated = "token." * (size * 1024 // 6)
            assignments = "token=a " * (size * 1024 // 8)
            payloads = {
                "path": ("https://example.com/" + "a" * (size * 1024), None),
                "api_path": ("https://example.com/api/" + "a" * (size * 1024), None),
                "field": ("ordinary_field=" + "a" * (size * 1024), None),
                "repeated": (repeated, None),
                "assignments": (
                    assignments,
                    "token=[REDACTED] " * (size * 1024 // 8),
                ),
                "authority": ("https://" + "a" * (size * 1024), "https://[REDACTED]"),
                "malformed": ("api_key" + "a" * (size * 1024), "[REDACTED]"),
            }
            for name, (text, expected) in payloads.items():
                filter_ = StreamingCredentialFilter()
                started = time.perf_counter()
                streamed = filter_.feed(text)
                buffered = filter_.buffered_characters
                rendered = streamed + filter_.finish()
                elapsed = time.perf_counter() - started
                assert rendered == (text if expected is None else expected)
                measurements.append((name, size, elapsed, buffered))
        print(json.dumps(measurements))
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    measurements = json.loads(completed.stdout)
    elapsed = {
        (name, size): duration
        for name, size, duration, _ in measurements
    }

    for name in {name for name, _, _, _ in measurements}:
        assert elapsed[name, 128] < 2.0
        assert elapsed[name, 512] < elapsed[name, 128] * 6 + 0.1
    assert all(buffered <= 2048 for _, _, _, buffered in measurements)
