"""The database CLI selects the dedicated test name explicitly."""

from types import SimpleNamespace

from app.infrastructure.persistence import cli


class _Settings:
    db_name = "wateragents"

    def model_copy(self, *, update):
        return SimpleNamespace(db_name=update["db_name"])


def test_test_database_flag_checks_separate_name_before_upgrade(monkeypatch, capsys):
    chosen = []
    monkeypatch.setattr(cli, "load_app_settings", _Settings)
    monkeypatch.setattr(cli, "upgrade_database", lambda settings: (
        chosen.append(settings.db_name), "20261008_03"
    )[1])
    monkeypatch.setenv("WATERAGENTS_TEST_DB_NAME", "wateragents")
    assert cli.run(["upgrade", "--test-database"]) == 1
    monkeypatch.delenv("WATERAGENTS_TEST_DB_NAME")
    assert cli.run(["upgrade", "--test-database"]) == 1
    assert chosen == []
    monkeypatch.setenv("WATERAGENTS_TEST_DB_NAME", "wateragents_test")
    assert cli.run(["upgrade", "--test-database"]) == 0
    assert chosen == ["wateragents_test"]
    assert "separate test database" in capsys.readouterr().out
