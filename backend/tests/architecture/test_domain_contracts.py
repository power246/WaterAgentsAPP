import ast
import inspect
import re
from pathlib import Path

import app.domain as domain

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
DOMAIN_ROOT = APP_ROOT / "domain"
DOMAIN_PACKAGE_BARRELS = {
    "app.domain",
    "app.domain.attribution",
    "app.domain.evidence",
    "app.domain.hydrology",
    "app.domain.inference",
    "app.domain.time",
}


def domain_sources() -> tuple[Path, ...]:
    return tuple(sorted(DOMAIN_ROOT.rglob("*.py")))


def public_class_definitions() -> dict[str, Path]:
    definitions: dict[str, Path] = {}
    for source_path in domain_sources():
        if source_path.name == "__init__.py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                assert node.name not in definitions, f"duplicate Domain class {node.name}"
                definitions[node.name] = source_path
    return definitions


def test_domain_classes_are_one_per_file_and_publicly_exported() -> None:
    definitions = public_class_definitions()
    classes_by_file: dict[Path, list[str]] = {}
    for class_name, source_path in definitions.items():
        classes_by_file.setdefault(source_path, []).append(class_name)

    assert all(len(class_names) == 1 for class_names in classes_by_file.values())
    assert set(definitions) <= set(domain.__all__)
    assert all(
        inspect.isclass(getattr(domain, class_name, None))
        for class_name in definitions
    )


def test_m1_contains_no_deferred_domain_types() -> None:
    forbidden_names = {"Waterbody"}
    violations: list[str] = []

    for source_path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{source_path}:{node.lineno}: {node.id}")
            elif isinstance(node, ast.ClassDef) and node.name in forbidden_names:
                violations.append(f"{source_path}:{node.lineno}: {node.name}")

    assert not violations, "\n".join(violations)


def test_domain_implementation_imports_leaf_modules_not_package_barrels() -> None:
    violations: list[str] = []

    for source_path in domain_sources():
        if source_path.name == "__init__.py":
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in DOMAIN_PACKAGE_BARRELS
            ):
                violations.append(f"{source_path}:{node.lineno}: {node.module}")

    assert not violations, "\n".join(violations)


def test_domain_uses_no_any_or_raw_datetime_or_float_model_fields() -> None:
    violations: list[str] = []

    for source_path in domain_sources():
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Any":
                violations.append(f"{source_path}:{node.lineno}: Any")
        for class_node in (
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ):
            for field in (
                node for node in class_node.body if isinstance(node, ast.AnnAssign)
            ):
                annotation = ast.unparse(field.annotation)
                for raw_type in ("datetime", "float"):
                    if re.search(rf"\b{raw_type}\b", annotation):
                        violations.append(
                            f"{source_path}:{field.lineno}: "
                            f"raw {raw_type} field {annotation}"
                        )

    assert not violations, "\n".join(violations)


def test_window_builder_does_not_use_formatted_local_time_keys() -> None:
    source_path = DOMAIN_ROOT / "time" / "window_builder.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
    formatting_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "isoformat", "strftime"}
    ]

    assert not formatting_calls
