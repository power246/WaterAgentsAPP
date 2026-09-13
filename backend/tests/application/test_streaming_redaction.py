import json

import pytest

from app.application.transcripts.streaming import StreamingCredentialFilter


def _render_at_every_split(text: str, private_values: tuple[str, ...] = ()) -> tuple[str, ...]:
    rendered = []
    for split in range(len(text) + 1):
        filter_ = StreamingCredentialFilter(private_values)
        rendered.append(filter_.feed(text[:split]) + filter_.feed(text[split:]) + filter_.finish())
    return tuple(rendered)


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        ('before api_key="秘密🔑value" after', ("秘密🔑value",)),
        ("before Authorization: Bearer synthetic-token\nafter", ("synthetic-token",)),
        ("before Cookie: sid=synthetic-cookie; HttpOnly\nafter", ("synthetic-cookie",)),
        (
            "before https://user:synthetic-password@provider.test/path after",
            ("synthetic-password",),
        ),
        ("before client_secret='quoted\\\"value' after", ('quoted\\"value',)),
    ],
)
def test_credentials_are_hidden_at_every_chunk_split(text: str, secrets: tuple[str, ...]) -> None:
    for rendered in _render_at_every_split(text, secrets):
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
