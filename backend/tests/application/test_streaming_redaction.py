import json

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

    assert filter_.feed("safe api_ke") == ""
    rendered = filter_.finish()
    assert rendered == "safe [REDACTED]"
