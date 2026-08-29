"""AST-based dependency rules shared by architecture tests."""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "my_code"
REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / PACKAGE_NAME

ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "foundation": frozenset(),
    "observability": frozenset({"foundation"}),
    "runtime": frozenset(
        {
            "agent",
            "context",
            "conversation",
            "model",
            "mcp",
            "observability",
            "permissions",
            "prompts",
            "providers",
            "sessions",
            "skills",
            "tasks",
            "tools",
            "workspace",
        }
    ),
    "tasks": frozenset(),
    "model": frozenset({"foundation"}),
    "workspace": frozenset(),
    "permissions": frozenset({"foundation", "model"}),
    "prompts": frozenset({"model"}),
    "auth": frozenset({"model"}),
    "config": frozenset({"auth", "model", "permissions"}),
    "conversation": frozenset({"foundation", "model"}),
    "context": frozenset({"conversation", "model", "prompts"}),
    "tools": frozenset(
        {
            "conversation",
            "foundation",
            "model",
            "permissions",
            "workspace",
        }
    ),
    "sessions": frozenset(
        {"context", "conversation", "foundation", "model", "prompts", "tools"}
    ),
    "providers": frozenset({"auth", "config", "foundation", "model"}),
    "agent": frozenset(
        {
            "context",
            "conversation",
            "foundation",
            "model",
            "permissions",
            "sessions",
            "tools",
        }
    ),
    "mcp": frozenset({"foundation", "model", "permissions", "tools"}),
    "skills": frozenset(
        {"context", "conversation", "foundation", "model", "permissions", "tools"}
    ),
    "features.file_mentions": frozenset(
        {"context", "conversation", "permissions", "tools", "workspace"}
    ),
    "features.todos": frozenset(
        {"context", "conversation", "foundation", "model", "permissions", "tools"}
    ),
    "features.subagents": frozenset(
        {
            "agent",
            "context",
            "conversation",
            "foundation",
            "model",
            "permissions",
            "prompts",
            "runtime",
            "sessions",
            "skills",
            "tasks",
            "tools",
            "features.background_tasks",
        }
    ),
    "features.background_tasks": frozenset({"agent", "foundation", "tasks", "tools"}),
    "features.plan_mode": frozenset({"agent", "prompts", "tools"}),
    "chat": frozenset(
        {
            "agent",
            "config",
            "context",
            "conversation",
            "features.file_mentions",
            "features.background_tasks",
            "features.todos",
            "foundation",
            "model",
            "permissions",
            "providers",
            "runtime",
            "sessions",
            "skills",
            "tasks",
            "tools",
        }
    ),
    "cli": frozenset({"config", "permissions"}),
    "tui": frozenset(
        {
            "chat",
            "config",
            "features.file_mentions",
            "features.todos",
            "model",
            "permissions",
            "providers",
            "sessions",
            "tools",
        }
    ),
    "bootstrap": frozenset(
        {
            "agent",
            "auth",
            "chat",
            "cli",
            "config",
            "context",
            "conversation",
            "features.file_mentions",
            "features.background_tasks",
            "features.plan_mode",
            "features.subagents",
            "features.todos",
            "foundation",
            "model",
            "mcp",
            "observability",
            "permissions",
            "prompts",
            "providers",
            "runtime",
            "sessions",
            "skills",
            "tasks",
            "tools",
            "tui",
            "workspace",
        }
    ),
}


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
class TemporaryViolation:
    """A module-edge migration exception that must disappear in its owner phase."""

    source: str
    target: str
    owner: str
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.target)


@dataclass(frozen=True, order=True)
class TechnicalLeak:
    """A provider/UI/storage technology used outside its owning module."""

    path: str
    line: int
    kind: str
    detail: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.kind, self.detail)

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.detail}"


@dataclass(frozen=True, order=True)
class TemporaryTechnicalLeak:
    path: str
    kind: str
    detail: str
    owner: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.kind, self.detail)


def _temporary_edges(
    owner: str,
    reason: str,
    *edges: tuple[str, str],
) -> tuple[TemporaryViolation, ...]:
    return tuple(
        TemporaryViolation(source, target, owner, reason) for source, target in edges
    )


# These are migration debt, not permissions for new architecture. Tests require
# exact equality with the current scan, so removed debt also makes the guard fail
# until this list is tightened.
TEMPORARY_DEPENDENCY_VIOLATIONS: tuple[TemporaryViolation, ...] = ()

TEMPORARY_TECHNICAL_LEAKS: tuple[TemporaryTechnicalLeak, ...] = ()

TEMPORARY_CYCLIC_COMPONENTS: frozenset[frozenset[str]] = frozenset()


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
                        path=path.relative_to(REPOSITORY_ROOT).as_posix(),
                        line=_line_number(node),
                        source=source,
                        target=target,
                        imported_module=imported_module,
                        imported_names=imported_names,
                    )
                )
    return tuple(sorted(set(edges)))


def dependency_violations(edges: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    return tuple(
        edge
        for edge in edges
        if edge.target not in ALLOWED_DEPENDENCIES.get(edge.source, frozenset())
    )


def public_import_violations(edges: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    """Return imports that bypass a declared semantic-module API."""

    exports: dict[str, frozenset[str] | None] = {}
    violations: list[ImportEdge] = []
    for edge in edges:
        suffix = edge.imported_module.removeprefix(f"{PACKAGE_NAME}.")
        if any(part.startswith("_") for part in suffix.split(".")):
            violations.append(edge)
            continue
        declared = exports.setdefault(
            edge.imported_module,
            _static_exports(edge.imported_module),
        )
        if declared is None or any(
            name == "*" or name not in declared for name in edge.imported_names
        ):
            violations.append(edge)
    return tuple(violations)


def foreign_reexports() -> tuple[ImportEdge, ...]:
    """Return public symbols re-exported from a different owner module."""

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
    for path in iter_python_files():
        module_name, is_package = _module_name_for_path(path)
        source = architecture_module(module_name)
        relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for imported_module in _imports_from_node(node, module_name, is_package):
                top_level = imported_module.split(".", 1)[0]
                if top_level in {"anthropic", "openai"} and source != "providers":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "provider-sdk",
                            imported_module,
                        )
                    )
                if top_level in {"prompt_toolkit", "rich"} and source != "tui":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "tui-framework",
                            imported_module,
                        )
                    )
                if top_level == "opentelemetry" and source != "observability":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "observability-sdk",
                            imported_module,
                        )
                    )
                if (
                    imported_module
                    in {
                        "my_code.bootstrap",
                    }
                    and source != "bootstrap"
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
                    imported_module == "my_code.application.state"
                    and relative_path
                    not in {
                        "src/my_code/bootstrap.py",
                        "src/my_code/chat/service.py",
                    }
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
                    and source != "sessions"
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
                and source != "sessions"
            ):
                leaks.append(
                    TechnicalLeak(
                        relative_path,
                        node.lineno,
                        "jsonl-path",
                        node.value,
                    )
                )
    return tuple(sorted(set(leaks)))


def graph_from_edges(edges: tuple[ImportEdge, ...]) -> dict[str, frozenset[str]]:
    graph: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        graph[edge.source].add(edge.target)
        graph.setdefault(edge.target, set())
    return {node: frozenset(targets) for node, targets in sorted(graph.items())}


def cyclic_components(
    graph: dict[str, frozenset[str]],
) -> frozenset[frozenset[str]]:
    """Return strongly connected components which contain a directed cycle."""

    next_index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: set[frozenset[str]] = set()

    def connect(node: str) -> None:
        nonlocal next_index
        indexes[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph.get(node, frozenset())):
            if target not in indexes:
                connect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        if len(component) > 1 or node in graph.get(node, frozenset()):
            components.add(frozenset(component))

    for node in sorted(graph):
        if node not in indexes:
            connect(node)
    return frozenset(components)


def cycle_paths(graph: dict[str, frozenset[str]]) -> frozenset[tuple[str, ...]]:
    """Return one deterministic complete cycle path for each cyclic component."""

    paths: set[tuple[str, ...]] = set()
    for component in cyclic_components(graph):

        def visit(
            start: str,
            node: str,
            path: tuple[str, ...],
            members: frozenset[str],
        ) -> tuple[str, ...] | None:
            for target in sorted(graph.get(node, frozenset()) & members):
                if target == start:
                    return path + (start,)
                if target not in path:
                    found = visit(start, target, path + (target,), members)
                    if found is not None:
                        return found
            return None

        for start in sorted(component):
            cycle = visit(start, start, (start,), component)
            if cycle is not None:
                paths.add(cycle)
                break
    return frozenset(paths)


def target_dependency_graph() -> dict[str, frozenset[str]]:
    return {module: targets for module, targets in ALLOWED_DEPENDENCIES.items()}


def violation_key(edge: ImportEdge) -> tuple[str, str]:
    return (edge.source, edge.target)


def format_edges(title: str, edges: tuple[ImportEdge, ...]) -> str:
    details = "\n".join(f"  - {edge.describe()}" for edge in edges)
    return f"{title}:\n{details}" if details else title


def format_cycles(title: str, cycles: frozenset[tuple[str, ...]]) -> str:
    details = "\n".join(f"  - {' -> '.join(cycle)}" for cycle in sorted(cycles))
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


def architecture_module(module_name: str) -> str:
    parts = module_name.split(".")
    if not parts or parts[0] != PACKAGE_NAME:
        return parts[0]
    if len(parts) == 1:
        return PACKAGE_NAME
    if parts[1] == "features" and len(parts) >= 3:
        return ".".join(parts[1:3])
    return parts[1]


def _imports_from_node(
    node: ast.AST, source_module: str, is_package: bool
) -> tuple[str, ...]:
    return tuple(
        imported_module
        for imported_module, _ in _import_records_from_node(
            node, source_module, is_package
        )
    )


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
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            continue
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in value.elts
        ):
            return None
        return frozenset(item.value for item in value.elts)  # type: ignore[union-attr]
    return None


def _path_for_module(module_name: str) -> Path | None:
    if not module_name.startswith(f"{PACKAGE_NAME}."):
        return None
    relative = Path(*module_name.split(".")[1:])
    module_path = SOURCE_ROOT / relative.with_suffix(".py")
    if module_path.exists():
        return module_path
    package_path = SOURCE_ROOT / relative / "__init__.py"
    return package_path if package_path.exists() else None
