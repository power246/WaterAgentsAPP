import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "backend-m1.yml"
SMOKE_SCRIPT_PATH = REPOSITORY_ROOT / "backend" / "scripts" / "health_smoke.py"


def test_ci_runs_install_tests_and_health_on_all_supported_operating_systems() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in workflow
    assert 'python-version: "3.11"' in workflow
    assert 'python -m pip install -e "./backend[dev]"' in workflow
    assert "python -m pytest backend/tests -q" in workflow
    assert "python backend/scripts/health_smoke.py" in workflow


def test_ci_commands_do_not_require_a_platform_specific_shell() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    platform_specific_commands = (
        "source ",
        "Activate.ps1",
        "python3 ",
        "rm ",
        "start /b",
    )

    assert not any(command in workflow for command in platform_specific_commands)


def test_health_smoke_uses_cross_platform_process_and_path_apis() -> None:
    source = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=SMOKE_SCRIPT_PATH)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    assert "sys.executable" in source
    assert "Path(__file__).resolve()" in source
    assert not any(
        any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
        for call in calls
    )
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("/tmp/", "/Users/", "/home/"))
        for node in ast.walk(tree)
    )
