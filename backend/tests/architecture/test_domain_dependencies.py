import ast
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[2] / "app" / "domain"


@dataclass(frozen=True)
class ImportViolation:
    source_path: Path
    line: int
    import_name: str

    def __str__(self) -> str:
        return (
            f"{self.source_path}:{self.line}: "
            f"forbidden import {self.import_name}"
        )


def is_allowed_domain_import(import_name: str) -> bool:
    root = import_name.split(".", maxsplit=1)[0]
    return (
        root in sys.stdlib_module_names
        or import_name == "pydantic"
        or import_name.startswith("pydantic.")
        or import_name == "app.domain"
        or import_name.startswith("app.domain.")
    )


def source_package(source_path: Path, domain_root: Path) -> str:
    relative_parent = source_path.relative_to(domain_root).parent
    suffix = ".".join(relative_parent.parts)
    return "app.domain" if not suffix else f"app.domain.{suffix}"


def resolve_from_import(
    node: ast.ImportFrom,
    source_path: Path,
    domain_root: Path,
) -> str:
    module = node.module or ""
    if not node.level:
        return module
    relative_name = f"{'.' * node.level}{module}"
    return importlib.util.resolve_name(
        relative_name,
        source_package(source_path, domain_root),
    )


def find_domain_import_violations(domain_root: Path) -> tuple[ImportViolation, ...]:
    violations: list[ImportViolation] = []
    for source_path in sorted(domain_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_from_import(node, source_path, domain_root)
                imports = [
                    base if alias.name == "*" else f"{base}.{alias.name}"
                    for alias in node.names
                ]
            else:
                continue
            violations.extend(
                ImportViolation(source_path, node.lineno, import_name)
                for import_name in imports
                if not is_allowed_domain_import(import_name)
            )
    return tuple(violations)


def test_domain_imports_follow_the_dependency_allowlist() -> None:
    violations = find_domain_import_violations(DOMAIN_ROOT)

    assert not violations, "\n".join(map(str, violations))


def test_dependency_allowlist_accepts_only_domain_dependencies() -> None:
    allowed = (
        "datetime",
        "pathlib.Path",
        "pydantic",
        "pydantic.types",
        "app.domain",
        "app.domain.time",
    )
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "httpx",
        "langchain",
        "langgraph",
        "openai",
        "anthropic",
        "app.api",
        "app.ports",
        "app.infrastructure",
        "app.application",
    )

    assert all(is_allowed_domain_import(name) for name in allowed)
    assert not any(is_allowed_domain_import(name) for name in forbidden)


def test_dependency_gate_reports_import_and_from_import_then_recovers(
    tmp_path: Path,
) -> None:
    temporary_domain = tmp_path / "domain"
    temporary_domain.mkdir()
    direct_import = temporary_domain / "direct.py"
    from_import = temporary_domain / "from_import.py"
    direct_import.write_text("import fastapi\n", encoding="utf-8")
    from_import.write_text("from app.ports import ClockPort\n", encoding="utf-8")

    violations = find_domain_import_violations(temporary_domain)

    assert [violation.line for violation in violations] == [1, 1]
    assert [violation.import_name for violation in violations] == [
        "fastapi",
        "app.ports.ClockPort",
    ]
    assert all(str(violation.source_path) in str(violation) for violation in violations)

    direct_import.write_text("from datetime import datetime\n", encoding="utf-8")
    from_import.write_text(
        "from app.domain import DomainModel\n",
        encoding="utf-8",
    )
    assert find_domain_import_violations(temporary_domain) == ()
