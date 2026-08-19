"""Production dependency direction and cycle checks."""

from .dependency_rules import (
    TEMPORARY_CYCLIC_COMPONENTS,
    TEMPORARY_DEPENDENCY_VIOLATIONS,
    collect_import_edges,
    cycle_paths,
    cyclic_components,
    dependency_violations,
    format_cycles,
    format_edges,
    graph_from_edges,
    target_dependency_graph,
    violation_key,
)


def test_target_dependency_table_is_acyclic() -> None:
    cycles = cycle_paths(target_dependency_graph())
    assert not cycles, format_cycles("target dependency table contains cycles", cycles)


def test_production_dependencies_match_registered_migration_debt() -> None:
    violations = dependency_violations(collect_import_edges())
    actual = {violation_key(edge) for edge in violations}
    registered = {item.key for item in TEMPORARY_DEPENDENCY_VIOLATIONS}
    assert actual == registered, format_edges(
        "unregistered or stale dependency violations", violations
    )


def test_production_dependency_cycles_match_registered_migration_debt() -> None:
    graph = graph_from_edges(collect_import_edges())
    components = cyclic_components(graph)
    assert components == TEMPORARY_CYCLIC_COMPONENTS, format_cycles(
        "unregistered or stale production dependency cycles", cycle_paths(graph)
    )


def test_cycle_report_contains_the_complete_path() -> None:
    graph = {
        "conversation": frozenset({"tools"}),
        "tools": frozenset({"permissions"}),
        "permissions": frozenset({"conversation"}),
    }
    assert cycle_paths(graph) == frozenset(
        {("conversation", "tools", "permissions", "conversation")}
    )
