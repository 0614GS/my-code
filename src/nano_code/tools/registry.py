"""Stable tool registration and lookup."""

from collections.abc import Iterable

from nano_code.tools.base import Tool, ToolDefinition


class ToolRegistry:
    """An immutable, deterministically ordered collection of tools."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        ordered = sorted(tools, key=lambda tool: tool.definition.name)
        by_name: dict[str, Tool] = {}
        for tool in ordered:
            name = tool.definition.name
            if name in by_name:
                raise ValueError(f"Duplicate tool name: {name}")
            by_name[name] = tool
        self._tools = tuple(ordered)
        self._by_name = by_name

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools)

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)
