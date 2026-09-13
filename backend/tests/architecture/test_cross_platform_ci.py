import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "backend-m1.yml"
SMOKE_SCRIPT_PATH = REPOSITORY_ROOT / "backend" / "scripts" / "health_smoke.py"


def test_ci_runs_install_tests_and_both_smokes_on_all_supported_operating_systems() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert runner in workflow
    assert 'python-version: "3.11"' in workflow
    assert 'python -m pip install -e "./backend[dev]"' in workflow
    assert "python -m pytest backend/tests -q" in workflow
    assert "python backend/scripts/health_smoke.py" in workflow
    assert "python backend/scripts/run_sse_smoke.py" in workflow
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
