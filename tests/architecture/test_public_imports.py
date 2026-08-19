"""Cross-module public API and technology ownership checks."""

import tomllib
from collections.abc import Mapping

from .dependency_rules import (
    REPOSITORY_ROOT,
    TEMPORARY_DEEP_IMPORTS,
    TEMPORARY_TECHNICAL_LEAKS,
    collect_import_edges,
    collect_technical_leaks,
    deep_imports,
    format_edges,
    violation_key,
)


def test_cross_module_imports_match_registered_deep_import_debt() -> None:
    violations = deep_imports(collect_import_edges())
    actual = {violation_key(edge) for edge in violations}
    registered = {item.key for item in TEMPORARY_DEEP_IMPORTS}
    assert actual == registered, format_edges(
        "unregistered or stale cross-module deep imports", violations
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
