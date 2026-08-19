"""Cross-module public API and technology ownership checks."""

import ast
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from . import dependency_rules
from .dependency_rules import (
    ALLOWED_DEPENDENCIES,
    REPOSITORY_ROOT,
    SOURCE_ROOT,
    TEMPORARY_TECHNICAL_LEAKS,
    ImportEdge,
    collect_import_edges,
    collect_technical_leaks,
    foreign_reexports,
    format_edges,
    public_import_violations,
)


def test_cross_module_imports_use_declared_semantic_apis() -> None:
    violations = public_import_violations(collect_import_edges())
    assert not violations, format_edges(
        "private modules or symbols missing from target __all__", violations
    )


def test_public_modules_do_not_reexport_foreign_capabilities() -> None:
    violations = foreign_reexports()
    assert not violations, format_edges("foreign capability re-exports", violations)


def test_public_import_guard_rejects_private_unlisted_and_wildcard_imports() -> None:
    edges = (
        ImportEdge("example.py", 1, "chat", "model", "nano_code.model._wire"),
        ImportEdge(
            "example.py",
            2,
            "chat",
            "model",
            "nano_code.model.request",
            ("NotPublic",),
        ),
        ImportEdge(
            "example.py",
            3,
            "chat",
            "model",
            "nano_code.model.request",
            ("*",),
        ),
    )
    assert public_import_violations(edges) == edges


def test_foreign_reexport_guard_reports_the_exporting_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "src" / "nano_code"
    source = source_root / "chat" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from nano_code.permissions.models import PermissionMode as Mode\n"
        "__all__ = ['Mode']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dependency_rules, "SOURCE_ROOT", source_root)
    monkeypatch.setattr(dependency_rules, "REPOSITORY_ROOT", tmp_path)

    violations = dependency_rules.foreign_reexports()

    assert len(violations) == 1
    assert violations[0].source == "chat"
    assert violations[0].target == "permissions"
    assert violations[0].imported_names == ("PermissionMode",)


def test_architecture_package_initializers_do_not_aggregate_apis() -> None:
    for module in ALLOWED_DEPENDENCIES:
        if module == "bootstrap":
            continue
        initializer = SOURCE_ROOT / Path(*module.split(".")) / "__init__.py"
        if not initializer.exists():
            continue
        tree = ast.parse(initializer.read_text(encoding="utf-8"))
        imports = tuple(
            node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        )
        assert not imports, (
            f"architecture package API must use semantic modules: {initializer}"
        )


def test_technology_leaks_match_registered_migration_debt() -> None:
    leaks = collect_technical_leaks()
    actual = {item.key for item in leaks}
    registered = {item.key for item in TEMPORARY_TECHNICAL_LEAKS}
    details = "\n".join(f"  - {leak.describe()}" for leak in leaks)
    assert actual == registered, f"unregistered or stale technology leaks:\n{details}"


def test_reference_snapshot_is_excluded_from_packaging_inputs() -> None:
    ignore_rules = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/claude-code/" in ignore_rules.splitlines()

    configuration = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    explicit_includes = _include_values(configuration)
    assert not any("claude-code" in value for value in explicit_includes)


def _include_values(value: object, key: str = "") -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            included
            for child_key, child_value in value.items()
            for included in _include_values(child_value, str(child_key))
        )
    if isinstance(value, list):
        if "include" not in key.lower() or "exclude" in key.lower():
            return ()
        return tuple(item for item in value if isinstance(item, str))
    return ()
