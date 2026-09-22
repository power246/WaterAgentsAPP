from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"


def test_compose_uses_pg17_loopback_healthcheck_and_named_volume() -> None:
    compose = (BACKEND_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "image: postgres:17" in compose
    assert '127.0.0.1:${WATERAGENTS_DB_PORT:-5432}:5432' in compose
    assert "pg_isready" in compose
    assert "wateragents-postgres-data:/var/lib/postgresql/data" in compose
    assert "volumes:\n  wateragents-postgres-data:" in compose


def test_example_environment_is_committed_but_real_environment_is_ignored() -> None:
    example = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    ignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "WATERAGENTS_DB_PASSWORD='replace-with-a-local-password'" in example
    assert ".env\n" in ignore
    assert "!.env.example" in ignore
