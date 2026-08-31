"""测试套件目录约束。"""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "my_code"
UNIT_ROOT = REPOSITORY_ROOT / "tests" / "unit"
TEMPORARY_TEST_PREFIXES = (
    "test_debug",
    "test_scratch",
    "test_temp",
    "test_tmp",
)


def test_unit_tests_mirror_source_owners() -> None:
    violations = unit_layout_violations(UNIT_ROOT, SOURCE_ROOT)

    assert not violations, "\n".join(str(path) for path in violations)


def test_layout_guard_rejects_root_unknown_and_temporary_tests(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "my_code"
    unit_root = tmp_path / "tests" / "unit"
    (source_root / "agent").mkdir(parents=True)
    (source_root / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "__pycache__").mkdir()
    (source_root / "bootstrap.py").write_text("", encoding="utf-8")
    (unit_root / "agent").mkdir(parents=True)
    (unit_root / "unknown").mkdir()
    (unit_root / "bootstrap").mkdir()
    root_test = unit_root / "test_loose.py"
    unknown_test = unit_root / "unknown" / "test_value.py"
    temporary_test = unit_root / "agent" / "test_tmp_case.py"
    valid_test = unit_root / "bootstrap" / "test_bootstrap.py"
    for path in (root_test, unknown_test, temporary_test, valid_test):
        path.write_text("", encoding="utf-8")

    assert unit_layout_violations(unit_root, source_root) == (
        temporary_test,
        root_test,
        unknown_test,
    )


def unit_layout_violations(unit_root: Path, source_root: Path) -> tuple[Path, ...]:
    """返回根目录散落、未知所有者和临时命名的单元测试。"""
    source_owners = {
        path.name if path.is_dir() else path.stem
        for path in source_root.iterdir()
        if (path.is_dir() and (path / "__init__.py").exists())
        or (path.suffix == ".py" and path.name != "__init__.py")
    }
    violations: set[Path] = set()
    for path in unit_root.rglob("test_*.py"):
        relative = path.relative_to(unit_root)
        if len(relative.parts) == 1:
            violations.add(path)
            continue
        if relative.parts[0] not in source_owners:
            violations.add(path)
        if path.stem.startswith(TEMPORARY_TEST_PREFIXES):
            violations.add(path)
    return tuple(sorted(violations))
