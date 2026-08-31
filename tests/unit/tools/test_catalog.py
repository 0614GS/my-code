"""Unit coverage for versioned, atomic tool catalog publication."""

import pytest

from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import ToolCatalog, ToolSourceId


def test_catalog_snapshots_source_and_keeps_stable_name_order() -> None:
    source_tools = list(reversed(builtin_tools()))
    source = ToolSourceId("test", "builtins")
    catalog = ToolCatalog()
    assert catalog.register_source(source, source_tools) == 1
    snapshot = catalog.snapshot()
    expected_names = tuple(sorted(tool.definition.name for tool in source_tools))

    source_tools.clear()

    assert snapshot.version == 1
    assert tuple(tool.definition.name for tool in snapshot.tools) == expected_names
    assert (
        tuple(definition.name for definition in snapshot.definitions) == expected_names
    )
    assert all(snapshot.get(name) is not None for name in expected_names)
    assert all(snapshot.source_for(name) == source for name in expected_names)


def test_conflicting_source_registration_is_atomic() -> None:
    tool = builtin_tools()[0]
    first = ToolSourceId("test", "first")
    conflicting = ToolSourceId("test", "conflicting")
    catalog = ToolCatalog()
    catalog.register_source(first, (tool,))
    before = catalog.snapshot()

    with pytest.raises(ValueError) as error:
        catalog.register_source(conflicting, (tool,))

    message = str(error.value)
    assert f"Duplicate tool name {tool.definition.name!r}" in message
    assert str(first) in message
    assert str(conflicting) in message
    assert catalog.version == before.version
    assert catalog.sources == (first,)
    assert catalog.snapshot() == before


def test_replace_and_unregister_publish_new_versions_without_mutating_snapshot() -> (
    None
):
    source = ToolSourceId("test", "replaceable")
    first, second = builtin_tools()[:2]
    catalog = ToolCatalog()
    catalog.register_source(source, (first,))
    old_snapshot = catalog.snapshot()

    assert catalog.replace_source(source, (second,)) == 2
    replacement = catalog.snapshot()
    assert catalog.unregister_source(source) is True
    empty = catalog.snapshot()

    assert old_snapshot.get(first.definition.name) is first
    assert old_snapshot.get(second.definition.name) is None
    assert replacement.version == 2
    assert replacement.get(first.definition.name) is None
    assert replacement.get(second.definition.name) is second
    assert empty.version == 3
    assert empty.tools == ()
    assert catalog.unregister_source(source) is False
    assert catalog.version == 3
