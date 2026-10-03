import ast
import json
import re
from pathlib import Path

from app.domain import ModelRole
from app.infrastructure.config import AppSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "backend-m1.yml"
SMOKE_SCRIPT_PATH = REPOSITORY_ROOT / "backend" / "scripts" / "health_smoke.py"
HISTORY_SMOKE_SCRIPT_PATH = (
    REPOSITORY_ROOT / "backend" / "scripts" / "run_history_smoke.py"
)
README_PATH = REPOSITORY_ROOT / "backend" / "README.md"
TEST_CONFIG_PATH = REPOSITORY_ROOT / "backend" / "tests" / "conftest.py"


def test_ci_runs_install_tests_and_both_smokes_on_all_supported_operating_systems() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in workflow
    assert 'python-version: "3.11"' in workflow
    assert "ikalnytskyi/action-setup-postgres@v8" in workflow
    assert 'postgres-version: "17"' in workflow
    assert "astral-sh/setup-uv@v6" in workflow
    assert "uv sync --project backend --extra dev --locked" in workflow
    assert "uv run --project backend --locked python -m pytest backend/tests -q" in workflow
    assert "WATERAGENTS_TEST_DB_NAME" in workflow
    assert "WATERAGENTS_DB_HOST" in workflow
    assert "WATERAGENTS_TEST_POSTGRES" not in workflow
    assert (
        "uv run --project backend --locked python backend/scripts/health_smoke.py"
        in workflow
    )
    assert (
        "uv run --project backend --locked python backend/scripts/run_history_smoke.py"
        in workflow
    )
    assert "HTTP_PROXY: http://127.0.0.1:1" in workflow
    assert 'NO_PROXY: ""' in workflow


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


def test_ci_model_catalog_matches_the_runtime_settings_schema() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"WATERAGENTS_MODEL_CATALOG:\s*'([^'\n]+)'",
        workflow,
    )

    assert match is not None
    settings = AppSettings(
        _env_file=None,
        model_catalog=json.loads(match.group(1)),
    )
    waterbody_capabilities = tuple(
        capability
        for capability in settings.model_catalog
        if ModelRole.WATERBODY in capability.roles
    )

    assert waterbody_capabilities


def test_tests_and_direct_async_smoke_share_windows_loop_initialization() -> None:
    test_config = TEST_CONFIG_PATH.read_text(encoding="utf-8")
    direct_smoke = HISTORY_SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")

    for source in (test_config, direct_smoke):
        assert "configure_asyncio_event_loop" in source


def test_history_smoke_passes_the_shared_loop_factory_to_uvicorn() -> None:
    source = HISTORY_SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=HISTORY_SMOKE_SCRIPT_PATH)
    popen_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    )
    command = ast.unparse(popen_call.args[0])

    assert "'--loop'" in command
    assert "uvicorn_loop()" in command


def test_health_smoke_uses_cross_platform_process_and_path_apis() -> None:
    source = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=SMOKE_SCRIPT_PATH)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    popen_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
        and call.func.attr == "Popen"
    )
    client_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "httpx"
        and call.func.attr == "Client"
    )
    popen_keywords = {keyword.arg: keyword.value for keyword in popen_call.keywords}
    client_keywords = {keyword.arg: keyword.value for keyword in client_call.keywords}
    command = popen_call.args[0]

    assert "sys.executable" in source
    assert "Path(__file__).resolve()" in source
    assert isinstance(command, ast.List)
    assert [ast.literal_eval(item) for item in command.elts[1:]] == ["-m", "app"]
    assert "PYTHONIOENCODING" in source
    assert "WATERAGENTS_HOST" in source
    assert "WATERAGENTS_PORT" in source
    assert "WATERAGENTS_LOG_LEVEL" in source
    assert ast.literal_eval(popen_keywords["encoding"]) == "utf-8"
    assert ast.literal_eval(popen_keywords["errors"]) == "replace"
    assert "env" in popen_keywords
    assert ast.literal_eval(client_keywords["trust_env"]) is False
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


def test_run_smoke_uses_dynamic_socket_and_cooperative_server_control() -> None:
    path = SMOKE_SCRIPT_PATH.with_name("run_sse_smoke.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        name.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for name in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imports.intersection({"signal", "subprocess", "os", "threading"})
    assert 'listener.bind(("127.0.0.1", 0))' in source
    assert "server.serve(sockets=[listener])" in source
    assert "server.ready.wait()" in source
    assert "server.should_exit = True" in source
    assert "await asyncio.gather(task, return_exceptions=True)" in source
    assert "trust_env=False" in source
    assert "workers=1" in source
    assert "sleep(" not in source


def test_history_smoke_restarts_two_independent_uvicorn_processes() -> None:
    source = HISTORY_SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=HISTORY_SMOKE_SCRIPT_PATH)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    popen_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
        and call.func.attr == "Popen"
    )
    command = ast.unparse(popen_call.args[0])
    keywords = {keyword.arg: keyword.value for keyword in popen_call.keywords}

    assert "sys.executable" in command
    for argument in (
        "'-m'",
        "'uvicorn'",
        "'run_history_smoke:create_smoke_app'",
        "'--factory'",
        "'--workers'",
        "'1'",
    ):
        assert argument in command
    assert ast.unparse(keywords["cwd"]) == "SCRIPT_ROOT"
    assert ast.unparse(keywords["env"]) == "environment"
    assert ast.literal_eval(keywords["encoding"]) == "utf-8"
    assert ast.literal_eval(keywords["errors"]) == "replace"
    assert "WATERAGENTS_DB_NAME" in source
    assert "PYTHONIOENCODING" in source
    assert "trust_env=False" in source
    assert source.count("settings=SMOKE_SETTINGS") == 4
    assert 'update={"model_catalog": SMOKE_SETTINGS.model_catalog}' in source
    assert source.count("async with running_application(settings)") == 2
    assert "assert first_process.poll() is not None" in source
    assert "assert second_process.pid != first_pid" in source
    assert "running_server" not in source
    assert not any(
        keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for call in calls
        for keyword in call.keywords
    )


def test_database_smokes_share_cross_platform_process_shutdown_fallback() -> None:
    health = SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")
    history = HISTORY_SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")

    for expected in (
        "process.terminate()",
        "except subprocess.TimeoutExpired:",
        "process.kill()",
        "process.wait(timeout=5)",
    ):
        assert expected in health
    assert "from health_smoke import available_loopback_port, stop_process" in history
    assert "stop_process(process)" in history
    assert "available_loopback_port()" in history


def test_readme_commands_share_the_activated_backend_working_directory() -> None:
    readme = README_PATH.read_text(encoding="utf-8")

    assert "every remaining command in this README is written for that directory" in readme
    assert "does not require `uv`" in readme
    assert "uv run" not in readme
    assert "python -m app" in readme
    assert "wateragents-db upgrade" in readme
    assert "wateragents-db check" in readme
    assert "python scripts/health_smoke.py" in readme
    assert "python scripts/run_history_smoke.py" in readme
    assert "python backend/scripts/" not in readme
    assert "--env-file .env -f compose.yaml" in readme


def test_database_smokes_use_an_explicit_isolated_schema() -> None:
    helper = (
        REPOSITORY_ROOT / "backend" / "scripts" / "test_database.py"
    ).read_text(encoding="utf-8")
    history = HISTORY_SMOKE_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "WATERAGENTS_TEST_DB_NAME" in helper
    assert "must differ from WATERAGENTS_DB_NAME" in helper
    assert "CREATE SCHEMA" in helper
    assert "DROP SCHEMA" in helper
    assert "PGOPTIONS" in helper
    assert "temporary_migrated_schema" in history
    assert "trust_env=False" in (
        REPOSITORY_ROOT / "backend" / "scripts" / "run_sse_smoke.py"
    ).read_text(encoding="utf-8")
