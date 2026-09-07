import ast
import re
import tomllib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].lower()


def test_project_metadata_has_only_m1_dependencies() -> None:
    with (BACKEND_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    project = pyproject["project"]
    production = {_requirement_name(item) for item in project["dependencies"]}
    development = {
        _requirement_name(item)
        for item in project["optional-dependencies"]["dev"]
    }

    assert project["requires-python"] == ">=3.11"
    assert production == {
        "fastapi",
        "pydantic",
        "pydantic-settings",
        "tzdata",
        "uvicorn",
    }
    assert development == {"httpx", "pytest"}


def test_application_source_avoids_platform_specific_runtime_assumptions() -> None:
    forbidden_imports = {
        "fcntl",
        "grp",
        "posix",
        "pwd",
        "resource",
        "subprocess",
        "termios",
    }
    forbidden_path_fragments = ("/Users/", "/home/", "/tmp/")
    drive_path = re.compile(r"^[A-Za-z]:[\\/]")
    violations: list[str] = []

    for source_path in APP_ROOT.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
                blocked = names & forbidden_imports
                if blocked:
                    violations.append(f"{source_path}: imports {sorted(blocked)}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                name = node.module.split(".", maxsplit=1)[0]
                if name in forbidden_imports:
                    violations.append(f"{source_path}: imports {name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(part in node.value for part in forbidden_path_fragments):
                    violations.append(f"{source_path}: hard-coded path {node.value!r}")
                if drive_path.match(node.value):
                    violations.append(f"{source_path}: hard-coded drive {node.value!r}")

    assert not violations, "\n".join(violations)
