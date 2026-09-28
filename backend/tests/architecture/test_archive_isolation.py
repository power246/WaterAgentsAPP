import re
import tomllib
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
GUARD_PATH = Path(__file__).resolve()
ARCHIVE_NAMES = ("backend" + "_bk", "frontend" + "_bk")
ARCHIVE_REFERENCE = re.compile(
    rf"\b(?:{'|'.join(map(re.escape, ARCHIVE_NAMES))})\b"
)


def guarded_files() -> tuple[Path, ...]:
    files: set[Path] = set()
    for root in (
        BACKEND_ROOT / "app",
        BACKEND_ROOT / "scripts",
        BACKEND_ROOT / "tests",
    ):
        files.update(root.rglob("*.py"))
    files.discard(GUARD_PATH)
    files.update(
        path
        for path in (
            BACKEND_ROOT / "pyproject.toml",
            BACKEND_ROOT / "uv.lock",
            BACKEND_ROOT / "compose.yaml",
            BACKEND_ROOT / ".env.example",
        )
        if path.is_file()
    )
    workflow_root = REPOSITORY_ROOT / ".github" / "workflows"
    if workflow_root.is_dir():
        files.update(path for path in workflow_root.rglob("*") if path.is_file())
    return tuple(sorted(files))


def archive_reference_violations(paths: tuple[Path, ...]) -> tuple[str, ...]:
    violations: list[str] = []
    for path in paths:
        try:
            display_path = path.relative_to(REPOSITORY_ROOT)
        except ValueError:
            display_path = path
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if ARCHIVE_REFERENCE.search(line):
                violations.append(
                    f"{display_path}:{line_number}: "
                    "references an archived system"
                )
    return tuple(violations)


def pytest_run_commands(workflow: Path) -> tuple[str, ...]:
    commands = []
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        directive = stripped.removeprefix("-").lstrip()
        if not directive.startswith("run:"):
            continue
        command = directive.removeprefix("run:").strip()
        if command in {"|", ">", "|-", ">-"}:
            indentation = len(line) - len(line.lstrip())
            command_lines = []
            for continuation in lines[index + 1:]:
                if continuation.strip() and len(continuation) - len(
                    continuation.lstrip()
                ) <= indentation:
                    break
                command_lines.append(continuation.strip())
            command = " ".join(command_lines)
        if "pytest" in command.split():
            commands.append(command)
    return tuple(commands)


def test_current_backend_and_ci_do_not_reference_archived_systems() -> None:
    violations = archive_reference_violations(guarded_files())

    assert not violations, "\n".join(violations)


def test_archive_gate_detects_import_open_enumeration_and_dependency_paths(
    tmp_path: Path,
) -> None:
    samples = (
        "import backend_bk.service\n",
        "Path('../frontend_bk/config.json').open()\n",
        "list((root / 'backend_bk').iterdir())\n",
        "legacy = { path = '../frontend_bk' }\n",
    )
    paths = []
    for index, sample in enumerate(samples):
        path = tmp_path / f"sample-{index}.txt"
        path.write_text(sample, encoding="utf-8")
        paths.append(path)

    violations = archive_reference_violations(tuple(paths))

    assert len(violations) == len(samples)


def test_pytest_collection_is_scoped_to_current_backend_tests() -> None:
    configuration = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert configuration["tool"]["pytest"]["ini_options"]["testpaths"] == [
        "tests"
    ]


def test_ci_pytest_commands_are_scoped_to_current_backend_tests() -> None:
    workflow_root = REPOSITORY_ROOT / ".github" / "workflows"
    commands = tuple(
        command
        for workflow in sorted(workflow_root.glob("*.y*ml"))
        for command in pytest_run_commands(workflow)
    )

    assert commands
    assert all("backend/tests" in command.split() for command in commands)


def test_ci_command_gate_reads_inline_and_block_commands(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "steps:\n"
        "  - run: python -m pytest backend/tests -q\n"
        "  - run: |\n"
        "      python -m pytest backend/tests/architecture\n"
        "      python -m compileall backend/app\n",
        encoding="utf-8",
    )

    assert pytest_run_commands(workflow) == (
        "python -m pytest backend/tests -q",
        "python -m pytest backend/tests/architecture python -m compileall backend/app",
    )


@pytest.mark.parametrize(
    "allowed_name",
    ("backend", "frontend", "backend_backup", "archived-backend-notes"),
)
def test_archive_gate_does_not_reject_unrelated_names(
    tmp_path: Path,
    allowed_name: str,
) -> None:
    path = tmp_path / "allowed.txt"
    path.write_text(allowed_name, encoding="utf-8")

    assert archive_reference_violations((path,)) == ()
