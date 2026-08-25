"""Versioned tool sources and immutable execution snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType

from my_code.model.request import ModelToolDefinition
from my_code.tools.base import Tool


@dataclass(frozen=True, slots=True, order=True)
class ToolSourceId:
    """Stable identity for one independently replaceable tool source."""

    kind: str
    name: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.name.strip():
            raise ValueError("Tool source kind and name must not be blank")

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


_STATIC_TOOL_SOURCE = ToolSourceId("static", "direct")


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """One Tool and the source responsible for its lifecycle."""

    source: ToolSourceId
    tool: Tool


@dataclass(frozen=True, slots=True)
class ToolCatalogSnapshot:
    """One immutable, internally consistent version of all registered Tools."""

    version: int
    registrations: tuple[ToolRegistration, ...]
    tools: tuple[Tool, ...]
    definitions: tuple[ModelToolDefinition, ...]
    _tools_by_name: Mapping[str, Tool]
    _sources_by_name: Mapping[str, ToolSourceId]

    @classmethod
    def from_registrations(
        cls,
        version: int,
        registrations: Iterable[ToolRegistration],
    ) -> ToolCatalogSnapshot:
        if version < 0:
            raise ValueError("Tool catalog version must not be negative")
        ordered = tuple(
            sorted(registrations, key=lambda item: item.tool.definition.name)
        )
        tools_by_name: dict[str, Tool] = {}
        sources_by_name: dict[str, ToolSourceId] = {}
        for registration in ordered:
            name = registration.tool.definition.name
            previous = sources_by_name.get(name)
            if previous is not None:
                raise ValueError(
                    f"Duplicate tool name {name!r} from {previous} "
                    f"and {registration.source}"
                )
            tools_by_name[name] = registration.tool
            sources_by_name[name] = registration.source
        tools = tuple(registration.tool for registration in ordered)
        return cls(
            version=version,
            registrations=ordered,
            tools=tools,
            definitions=tuple(tool.definition for tool in tools),
            _tools_by_name=MappingProxyType(tools_by_name),
            _sources_by_name=MappingProxyType(sources_by_name),
        )

    @classmethod
    def from_tools(
        cls,
        tools: Iterable[Tool],
        *,
        source: ToolSourceId = _STATIC_TOOL_SOURCE,
        version: int = 0,
    ) -> ToolCatalogSnapshot:
        """Build a standalone snapshot for a fixed tool subset."""

        return cls.from_registrations(
            version,
            (ToolRegistration(source, tool) for tool in tools),
        )

    def get(self, name: str) -> Tool | None:
        return self._tools_by_name.get(name)

    def source_for(self, name: str) -> ToolSourceId | None:
        return self._sources_by_name.get(name)

    def as_mapping(self) -> Mapping[str, Tool]:
        return self._tools_by_name

    def select(self, names: Set[str]) -> ToolCatalogSnapshot:
        """Return the same catalog version narrowed to an explicit name set."""

        return self.from_registrations(
            self.version,
            (
                registration
                for registration in self.registrations
                if registration.tool.definition.name in names
            ),
        )


class ToolCatalog:
    """Mutable application-lifetime source registry with atomic publication."""

    def __init__(self) -> None:
        self._version = 0
        self._sources: dict[ToolSourceId, tuple[ToolRegistration, ...]] = {}

    @property
    def version(self) -> int:
        return self._version

    @property
    def sources(self) -> tuple[ToolSourceId, ...]:
        return tuple(sorted(self._sources))

    def register_source(
        self,
        source: ToolSourceId,
        tools: Iterable[Tool],
    ) -> int:
        if source in self._sources:
            raise ValueError(f"Tool source is already registered: {source}")
        return self._publish(source, tools)

    def replace_source(
        self,
        source: ToolSourceId,
        tools: Iterable[Tool],
    ) -> int:
        """Create or atomically replace a complete source contribution."""

        return self._publish(source, tools)

    def unregister_source(self, source: ToolSourceId) -> bool:
        if source not in self._sources:
            return False
        candidate = dict(self._sources)
        del candidate[source]
        _snapshot(self._version + 1, candidate)
        self._sources = candidate
        self._version += 1
        return True

    def snapshot(self) -> ToolCatalogSnapshot:
        return _snapshot(self._version, self._sources)

    def _publish(self, source: ToolSourceId, tools: Iterable[Tool]) -> int:
        registrations = tuple(ToolRegistration(source, tool) for tool in tools)
        candidate = dict(self._sources)
        candidate[source] = registrations
        _snapshot(self._version + 1, candidate)
        self._sources = candidate
        self._version += 1
        return self._version


def _snapshot(
    version: int,
    sources: Mapping[ToolSourceId, tuple[ToolRegistration, ...]],
) -> ToolCatalogSnapshot:
    return ToolCatalogSnapshot.from_registrations(
        version,
        (
            registration
            for source in sorted(sources)
            for registration in sources[source]
        ),
    )


__all__ = [
    "ToolCatalog",
    "ToolCatalogSnapshot",
    "ToolRegistration",
    "ToolSourceId",
]
