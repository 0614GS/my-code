"""AST-based dependency rules shared by architecture tests."""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "nano_code"
REPOSITORY_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / PACKAGE_NAME

ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "model": frozenset(),
    "workspace": frozenset(),
    "permissions": frozenset({"model"}),
    "prompts": frozenset({"model"}),
    "auth": frozenset({"model"}),
    "config": frozenset({"model"}),
    "conversation": frozenset({"model"}),
    "context": frozenset({"conversation", "model", "prompts"}),
    "tools": frozenset({"conversation", "model", "permissions", "workspace"}),
    "sessions": frozenset({"conversation", "model", "tools"}),
    "providers": frozenset({"auth", "config", "model"}),
    "agent": frozenset({"context", "conversation", "model", "sessions", "tools"}),
    "features.file_mentions": frozenset(
        {"context", "permissions", "tools", "workspace"}
    ),
    "features.todos": frozenset({"context", "conversation", "model", "tools"}),
    "features.subagents": frozenset({"agent", "context", "tools"}),
    "features.plan_mode": frozenset({"agent", "prompts", "tools"}),
    "chat": frozenset(
        {
            "agent",
            "context",
            "features.file_mentions",
            "features.todos",
            "model",
            "permissions",
            "providers",
            "sessions",
            "tools",
        }
    ),
    "cli": frozenset({"auth", "chat", "config"}),
    "tui": frozenset({"chat"}),
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
            "features.plan_mode",
            "features.subagents",
            "features.todos",
            "model",
            "permissions",
            "prompts",
            "providers",
            "sessions",
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
TEMPORARY_DEPENDENCY_VIOLATIONS: tuple[TemporaryViolation, ...] = (
    *_temporary_edges(
        "phase-4",
        "Tool presentation and permission requests use legacy concrete owners.",
        ("permissions", "tools"),
        ("tools", "agent"),
        ("workspace", "permissions"),
    ),
    *_temporary_edges(
        "phase-5",
        "Feature state and adapters have not moved behind feature boundaries.",
        ("agent", "features.todos"),
        ("features.file_mentions", "chat"),
        ("context", "agent"),
        ("tools", "features.todos"),
    ),
    *_temporary_edges(
        "phase-6",
        "Hosts and Chat still bypass the final Chat service boundary.",
        ("cli", "agent"),
        ("cli", "model"),
        ("cli", "permissions"),
        ("cli", "providers"),
        ("cli", "sessions"),
        ("cli", "tui"),
        ("tui", "features.todos"),
        ("tui", "model"),
        ("tui", "permissions"),
        ("tui", "providers"),
        ("tui", "sessions"),
        ("tui", "tools"),
    ),
    *_temporary_edges(
        "phase-7",
        "Legacy Core and Constants mix configuration with composition.",
        ("bootstrap", "core"),
        ("chat", "core"),
        ("cli", "bootstrap"),
        ("cli", "core"),
        ("core", "auth"),
        ("core", "model"),
        ("core", "permissions"),
        ("core", "providers"),
        ("permissions", "core"),
        ("prompts", "constants"),
        ("providers", "core"),
    ),
)

TEMPORARY_DEEP_IMPORTS: tuple[TemporaryViolation, ...] = (
    *_temporary_edges(
        "phase-4",
        "Permission and Tool public APIs are not final.",
        ("chat", "permissions"),
        ("permissions", "tools"),
        ("tools", "permissions"),
        ("tools", "agent"),
        ("workspace", "permissions"),
    ),
    *_temporary_edges(
        "phase-5",
        "Context and feature callers still reach into implementation modules.",
        ("agent", "context"),
        ("agent", "features.todos"),
        ("chat", "context"),
        ("chat", "features.todos"),
        ("features.file_mentions", "context"),
        ("context", "agent"),
        ("features.file_mentions", "permissions"),
        ("features.todos", "context"),
        ("tools", "features.todos"),
        ("tui", "features.todos"),
        ("tui", "permissions"),
    ),
    *_temporary_edges(
        "phase-6",
        "Legacy application.chat and host imports bypass final module roots.",
        ("chat", "agent"),
        ("chat", "providers"),
        ("features.file_mentions", "chat"),
        ("tui", "chat"),
        ("tui", "providers"),
    ),
    *_temporary_edges(
        "phase-7",
        "Legacy Core, Constants, and composition imports have no final root API.",
        ("bootstrap", "chat"),
        ("bootstrap", "cli"),
        ("bootstrap", "context"),
        ("bootstrap", "core"),
        ("bootstrap", "features.todos"),
        ("bootstrap", "permissions"),
        ("bootstrap", "providers"),
        ("bootstrap", "tools"),
        ("cli", "bootstrap"),
        ("cli", "providers"),
        ("core", "permissions"),
        ("core", "providers"),
        ("permissions", "core"),
        ("prompts", "constants"),
        ("providers", "core"),
    ),
)

TEMPORARY_TECHNICAL_LEAKS: tuple[TemporaryTechnicalLeak, ...] = (
    TemporaryTechnicalLeak(
        path="src/nano_code/cli/main.py",
        kind="composition-root",
        detail="nano_code.core.bootstrap",
        owner="phase-7",
        reason="The script entry point still imports the legacy composition root.",
    ),
    TemporaryTechnicalLeak(
        path="src/nano_code/core/paths.py",
        kind="jsonl-path",
        detail=".jsonl",
        owner="phase-7",
        reason="Session transcript path construction still lives in Core.",
    ),
)

TEMPORARY_CYCLIC_COMPONENTS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset(
            {
                "agent",
                "context",
                "core",
                "features.todos",
                "permissions",
                "providers",
                "sessions",
                "tools",
                "workspace",
            }
        ),
        frozenset({"chat", "features.file_mentions"}),
        frozenset({"bootstrap", "cli"}),
    }
)


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
            for imported_module in _imports_from_node(node, module_name, is_package):
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
                    )
                )
    return tuple(sorted(set(edges)))


def dependency_violations(edges: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    return tuple(
        edge
        for edge in edges
        if edge.target not in ALLOWED_DEPENDENCIES.get(edge.source, frozenset())
    )


def deep_imports(edges: tuple[ImportEdge, ...]) -> tuple[ImportEdge, ...]:
    return tuple(
        edge
        for edge in edges
        if edge.imported_module != public_module_name(edge.target)
    )


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
                if top_level == "textual" and source != "tui":
                    leaks.append(
                        TechnicalLeak(
                            relative_path,
                            _line_number(node),
                            "tui-framework",
                            imported_module,
                        )
                    )
                if (
                    imported_module
                    in {
                        "nano_code.bootstrap",
                        "nano_code.core.bootstrap",
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
                    imported_module == "nano_code.sessions.records"
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


def public_module_name(module: str) -> str:
    return f"{PACKAGE_NAME}.{module}"


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
    if parts[1:3] == ["core", "bootstrap"]:
        return "bootstrap"
    if parts[1] == "features" and len(parts) >= 3:
        return ".".join(parts[1:3])
    if parts[1:3] == ["application", "chat"]:
        return "chat"
    return parts[1]


def _imports_from_node(
    node: ast.AST, source_module: str, is_package: bool
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()
    if node.level == 0:
        return (node.module,) if node.module is not None else ()

    source_parts = source_module.split(".")
    package_parts = source_parts if is_package else source_parts[:-1]
    keep = len(package_parts) - (node.level - 1)
    if keep < 1:
        return ()
    resolved = package_parts[:keep]
    if node.module is not None:
        resolved.extend(node.module.split("."))
    return (".".join(resolved),)
