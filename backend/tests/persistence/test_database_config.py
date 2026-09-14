import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.infrastructure.config.settings import AppSettings, BACKEND_ENV_FILE
from app.infrastructure.persistence import cli
from app.infrastructure.persistence.database import (
    DatabaseConfigurationError,
    IncompatibleSchemaError,
    MissingSchemaError,
    MultipleSchemaHeadsError,
    NewerSchemaError,
    OutdatedSchemaError,
    _validate_database_revisions,
    alembic_config,
    database_url,
    expected_schema_head,
    require_database_password,
)
from scripts import test_database


def test_database_settings_are_cwd_independent_and_environment_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WATERAGENTS_DB_HOST", "db.example.internal")
    monkeypatch.setenv("WATERAGENTS_DB_PORT", "6543")
    monkeypatch.setenv("WATERAGENTS_DB_SSLMODE", " VERIFY-FULL ")

    settings = AppSettings(db_password="test")

    assert Path(BACKEND_ENV_FILE).is_absolute()
    assert settings.db_host == "db.example.internal"
    assert settings.db_port == 6543
    assert settings.db_sslmode == "verify-full"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_env_file_supports_unicode_space_paths_and_line_endings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    newline: str,
) -> None:
    for name in (
        "WATERAGENTS_DB_HOST",
        "WATERAGENTS_DB_PASSWORD",
        "WATERAGENTS_DB_SSLMODE",
    ):
        monkeypatch.delenv(name, raising=False)
    config_directory = tmp_path / "配置 with space"
    config_directory.mkdir()
    env_file = config_directory / ".env"
    env_file.write_bytes(
        newline.join(
            (
                "WATERAGENTS_DB_HOST=db.from.file",
                "WATERAGENTS_DB_PASSWORD='  p@:/#%$ word  '",
                "WATERAGENTS_DB_SSLMODE=require",
                "",
            )
        ).encode("utf-8")
    )
    unrelated_cwd = tmp_path / "另一个 cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    settings = AppSettings(_env_file=env_file)

    assert settings.db_host == "db.from.file"
    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == "  p@:/#%$ word  "
    assert settings.db_sslmode == "require"


def test_database_smoke_rejects_business_database_loaded_from_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WATERAGENTS_DB_NAME=business_from_file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WATERAGENTS_DB_NAME", raising=False)
    monkeypatch.setenv("WATERAGENTS_TEST_DB_NAME", "business_from_file")
    monkeypatch.setattr(
        test_database,
        "AppSettings",
        lambda: AppSettings(_env_file=env_file),
    )

    with pytest.raises(
        RuntimeError,
        match="WATERAGENTS_TEST_DB_NAME must differ from WATERAGENTS_DB_NAME",
    ):
        test_database.require_test_database_settings()


def test_database_smoke_preserves_business_connection_for_distinct_test_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "WATERAGENTS_DB_HOST=db.from.file",
                "WATERAGENTS_DB_PORT=6543",
                "WATERAGENTS_DB_NAME=business_from_file",
                "WATERAGENTS_DB_USER=smoke_user",
                "WATERAGENTS_DB_PASSWORD=smoke_password",
                "",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "WATERAGENTS_DB_HOST",
        "WATERAGENTS_DB_PORT",
        "WATERAGENTS_DB_NAME",
        "WATERAGENTS_DB_USER",
        "WATERAGENTS_DB_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATERAGENTS_TEST_DB_NAME", "wateragents_test")
    monkeypatch.setattr(
        test_database,
        "AppSettings",
        lambda: AppSettings(_env_file=env_file),
    )

    settings = test_database.require_test_database_settings()

    assert settings.db_host == "db.from.file"
    assert settings.db_port == 6543
    assert settings.db_name == "wateragents_test"
    assert settings.db_user == "smoke_user"
    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == "smoke_password"


def test_database_url_preserves_and_encodes_special_password() -> None:
    password = "  p@:/#%$ word  "
    settings = AppSettings(
        db_user="user@tenant",
        db_password=password,
        db_sslmode="verify-ca",
        db_sslrootcert=" /certs/root ca.pem ",
    )

    url = database_url(settings)
    rendered = url.render_as_string(hide_password=False)

    assert settings.db_password is not None
    assert settings.db_password.get_secret_value() == password
    assert url.password == password
    assert url.query == {
        "sslmode": "verify-ca",
        "sslrootcert": "/certs/root ca.pem",
    }
    assert "p%40%3A%2F%23%25%24" in rendered
    assert password not in repr(settings)


def test_missing_or_empty_database_password_has_safe_error() -> None:
    for password in (None, ""):
        settings = AppSettings(db_password=password)
        with pytest.raises(DatabaseConfigurationError, match="DB_PASSWORD is required"):
            require_database_password(settings)


def test_whitespace_only_password_is_not_trimmed_or_rejected() -> None:
    settings = AppSettings(db_password="   ")
    assert require_database_password(settings) == "   "


def test_invalid_transcript_thresholds_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be even"):
        AppSettings(db_password="x", transcript_preview_bytes=3)
    with pytest.raises(ValidationError, match="must be smaller"):
        AppSettings(
            db_password="x",
            transcript_preview_bytes=64,
            transcript_inline_max_bytes=64,
        )


def test_alembic_has_exactly_one_packaged_head() -> None:
    config = alembic_config(AppSettings(db_password="x"))
    assert Path(config.get_main_option("script_location")).is_absolute()
    assert expected_schema_head(config) == "20260913_01"


class _Revision:
    def __init__(self, revision: str, down_revision: str | None = None) -> None:
        self.revision = revision
        self.down_revision = down_revision


class _Script:
    def __init__(self) -> None:
        self._revisions = {
            "20260913_00": _Revision("20260913_00"),
            "20260913_01": _Revision("20260913_01", "20260913_00"),
            "20260913_02": _Revision("20260913_02", "20260913_01"),
        }

    def get_revision(self, revision: str) -> _Revision:
        try:
            return self._revisions[revision]
        except KeyError as error:
            raise LookupError(revision) from error


@pytest.mark.parametrize("revisions", [(), ("a", "b")])
def test_missing_and_multiple_database_heads_fail(revisions: tuple[str, ...]) -> None:
    error = MultipleSchemaHeadsError if revisions else MissingSchemaError
    with pytest.raises(error):
        _validate_database_revisions(revisions, "20260913_01", _Script())  # type: ignore[arg-type]


def test_old_new_and_unknown_database_revisions_are_distinct() -> None:
    script = _Script()
    with pytest.raises(OutdatedSchemaError):
        _validate_database_revisions(
            ("20260913_00",), "20260913_01", script  # type: ignore[arg-type]
        )
    with pytest.raises(NewerSchemaError):
        _validate_database_revisions(
            ("20260913_02",), "20260913_01", script  # type: ignore[arg-type]
        )
    with pytest.raises(IncompatibleSchemaError):
        _validate_database_revisions(
            ("unrelated",), "20260913_01", script  # type: ignore[arg-type]
        )


def test_cli_missing_password_fails_without_echoing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("WATERAGENTS_DB_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "AppSettings", lambda: AppSettings(_env_file=None))
    assert cli.run(["check"]) == 1
    output = capsys.readouterr().out
    assert "DB_PASSWORD is required" in output
    assert "postgresql" not in output


def test_formal_entrypoint_requires_database_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.__main__ as entrypoint

    monkeypatch.delenv("WATERAGENTS_DB_PASSWORD", raising=False)
    monkeypatch.setattr(
        entrypoint,
        "AppSettings",
        lambda: AppSettings(_env_file=None),
    )
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start"),
    )
    with pytest.raises(DatabaseConfigurationError, match="DB_PASSWORD is required"):
        entrypoint.main()


def test_selector_policy_is_not_changed_inside_a_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.infrastructure.runtime import event_loop

    monkeypatch.setattr(event_loop.sys, "platform", "win32")

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="before startup"):
            event_loop.configure_asyncio_event_loop()

    asyncio.run(exercise())
