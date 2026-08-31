"""Source-level architecture rules that Tach cannot express precisely."""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "my_code"
REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / PACKAGE_NAME
TACH_CONFIG = REPOSITORY_ROOT / "tach.toml"


@dataclass(frozen=True, order=True)
class ImportEdge:
    """One production import resolved to architectural modules."""

    path: str
    line: int
    source: str
    target: str
    imported_module: str
    imported_names: tuple[str, ...] = ()

    def describe(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.source} -> {self.target} "
            f"({self.imported_module})"
        )


@dataclass(frozen=True, order=True)
class TechnicalLeak:
    """A provider/UI/storage technology used outside its owning module."""

    path: str
    line: int
    kind: str
    detail: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.detail}"


def configured_module_paths() -> tuple[str, ...]:
    """Read Tach boundaries, ordered for deterministic longest-prefix matching."""

    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    paths = (
        module["path"]
        for module in config.get("modules", ())
        if isinstance(module, dict)
        and isinstance(module.get("path"), str)
        and module["path"] != "<root>"
    )
    return tuple(sorted(paths, key=lambda path: (-len(path.split(".")), path)))


def architecture_module(module_name: str) -> str:
    for boundary in configured_module_paths():
        if module_name == boundary or module_name.startswith(f"{boundary}."):
            return boundary
    return "<root>"


def iter_python_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in SOURCE_ROOT.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )


def collect_import_edges() -> tuple[ImportEdge, ...]:
    edges: list[ImportEdge] = []
    for path in iter_python_files():
        module_name, is_package = _module_name_for_path(path)
        source = architecture_module(module_name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for imported_module, imported_names in _import_records_from_node(
                node, module_name, is_package
            ):
                if not imported_module.startswith(f"{PACKAGE_NAME}."):
                    continue
                target = architecture_module(imported_module)
                if source == target:
                    continue
                edges.append(
                    ImportEdge(
                        path.relative_to(REPOSITORY_ROOT).as_posix(),
                        _line_number(node),
                        source,
                        target,
                        imported_module,
                        imported_names,
                    )
                )
    return tuple(sorted(set(edges)))


def public_import_violations(edges: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    """Return cross-boundary imports that bypass a static semantic-module API."""

    exports: dict[str, frozenset[str] | None] = {}
    violations: list[ImportEdge] = []
    for edge in edges:
        suffix = edge.imported_module.removeprefix(f"{PACKAGE_NAME}.")
        if any(part.startswith("_") for part in suffix.split(".")):
            violations.append(edge)
            continue
        declared = exports.setdefault(
            edge.imported_module, _static_exports(edge.imported_module)
        )
        if declared is None or any(
            name == "*" or name not in declared for name in edge.imported_names
        ):
            violations.append(edge)
    return tuple(violations)


def foreign_reexports() -> tuple[ImportEdge, ...]:
    """Return public symbols re-exported from a different owner boundary."""

    violations: list[ImportEdge] = []
    for path in iter_python_files():
        module_name, is_package = _module_name_for_path(path)
        source = architecture_module(module_name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exported = _exports_from_tree(tree)
        if exported is None:
            continue
        for node in tree.body:
            for imported_module, _ in _import_records_from_node(
                node, module_name, is_package
            ):
                target = architecture_module(imported_module)
                if target == source:
                    continue
                aliases = (
                    node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else ()
                )
                for alias in aliases:
                    local_name = alias.asname or alias.name
                    if local_name not in exported:
                        continue
                    violations.append(
                        ImportEdge(
                            path.relative_to(REPOSITORY_ROOT).as_posix(),
                            _line_number(node),
                            source,
                            target,
                            imported_module,
                            (alias.name,),
                        )
                    )
    return tuple(sorted(set(violations)))


def collect_technical_leaks() -> tuple[TechnicalLeak, ...]:
    leaks: list[TechnicalLeak] = []
    app_state_owners = {
        "src/my_code/bootstrap.py",
        "src/my_code/application/service.py",
        "src/my_code/runtime/state.py",
    }
    for path in iter_python_files():
        module_name, is_package = _module_name_for_path(path)
        source = architecture_module(module_name)
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for imported_module, imported_names in _import_records_from_node(
                node, module_name, is_package
            ):
                top_level = imported_module.split(".", 1)[0]
                if (
                    top_level in {"anthropic", "openai"}
                    and source != "my_code.providers"
                ):
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "provider-sdk",
                            imported_module,
                        )
                    )
                if top_level in {"prompt_toolkit", "rich"} and source != "my_code.tui":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "tui-framework",
                            imported_module,
                        )
                    )
                if top_level == "opentelemetry" and source != "my_code.observability":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "observability-sdk",
                            imported_module,
                        )
                    )
                if (
                    imported_module == "my_code.bootstrap"
                    and source != "my_code.bootstrap"
                ):
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "composition-root",
                            imported_module,
                        )
                    )
                if (
                    imported_module == "my_code.runtime.state"
                    and "AppState" in imported_names
                    and relative_path not in app_state_owners
                ):
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "app-state",
                            imported_module,
                        )
                    )
                if (
                    imported_module
                    in {
                        "my_code.sessions._records",
                        "my_code.sessions._codec",
                        "my_code.sessions._store",
                    }
                    and source != "my_code.sessions"
                ):
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "jsonl-record",
                            imported_module,
                        )
                    )
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ".jsonl" in node.value
                and source != "my_code.sessions"
            ):
                leaks.append(
                    TechnicalLeak(relative_path, node.lineno, "jsonl-path", node.value)
                )
    return tuple(sorted(set(leaks)))


def format_edges(title: str, edges: tuple[ImportEdge, ...]) -> str:
    details = "\n".join(f"  - {edge.describe()}" for edge in edges)
    return f"{title}:\n{details}" if details else title


def _module_name_for_path(path: Path) -> tuple[str, bool]:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    parts = (PACKAGE_NAME, *relative.parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def _line_number(node: ast.AST) -> int:
    line = getattr(node, "lineno", 0)
    return line if isinstance(line, int) else 0


def _import_records_from_node(
    node: ast.AST, source_module: str, is_package: bool
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if isinstance(node, ast.Import):
        return tuple((alias.name, ()) for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()
    imported_names = tuple(alias.name for alias in node.names)
    if node.level == 0:
        return ((node.module, imported_names),) if node.module is not None else ()
    source_parts = source_module.split(".")
    package_parts = source_parts if is_package else source_parts[:-1]
    keep = len(package_parts) - (node.level - 1)
    if keep < 1:
        return ()
    resolved = package_parts[:keep]
    if node.module is not None:
        resolved.extend(node.module.split("."))
    return ((".".join(resolved), imported_names),)


def _static_exports(module_name: str) -> frozenset[str] | None:
    path = _path_for_module(module_name)
    if path is None:
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _exports_from_tree(tree)


def _exports_from_tree(tree: ast.Module) -> frozenset[str] | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "__all__":
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        exports: set[str] = set()
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            exports.add(item.value)
        return frozenset(exports)
    return None


def _path_for_module(module_name: str) -> Path | None:
    if module_name == PACKAGE_NAME:
        path = SOURCE_ROOT / "__init__.py"
    else:
        suffix = module_name.removeprefix(f"{PACKAGE_NAME}.")
        base = SOURCE_ROOT.joinpath(*suffix.split("."))
        path = base.with_suffix(".py")
        if not path.exists():
            path = base / "__init__.py"
    return path if path.exists() else None
