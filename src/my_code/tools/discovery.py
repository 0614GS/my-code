"""Provider-neutral tool discovery records and immutable step exposure."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from my_code.conversation.attachments import (
    ToolDiscoveryAttachment,
    ToolDiscoveryDefinition,
    ToolDiscoveryInvalidationAttachment,
)
from my_code.conversation.models import AttachmentMessage, ConversationEntry
from my_code.model.request import ModelToolDefinition
from my_code.model.tool_search import ToolSearchMode
from my_code.tools.base import Tool, ToolExposure
from my_code.tools.catalog import ToolCatalogSnapshot

TOOL_SEARCH_NAME = "ToolSearch"
INVOKE_SEARCHED_TOOL_NAME = "InvokeSearchedTool"


def definition_fingerprint(definition: ModelToolDefinition) -> str:
    canonical = json.dumps(
        {
            "name": definition.name,
            "description": definition.description,
            "input_schema": definition.input_schema,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def discovery_definition(tool: Tool) -> ToolDiscoveryDefinition:
    definition = tool.definition
    return ToolDiscoveryDefinition(
        definition.name,
        definition.description,
        definition.input_schema,
        definition_fingerprint(definition),
    )


def restored_discoveries(
    entries: Iterable[ConversationEntry],
) -> Mapping[str, ToolDiscoveryDefinition]:
    found: dict[str, ToolDiscoveryDefinition] = {}
    for entry in entries:
        if not isinstance(entry, AttachmentMessage):
            continue
        payload = entry.payload
        if isinstance(payload, ToolDiscoveryAttachment):
            found.update((item.name, item) for item in payload.definitions)
        elif isinstance(payload, ToolDiscoveryInvalidationAttachment):
            for name in payload.names:
                found.pop(name, None)
    return MappingProxyType(found)


@dataclass(frozen=True, slots=True)
class ToolExposureSnapshot:
    """All execution tools plus the exact direct/model exposure for one step."""

    catalog: ToolCatalogSnapshot
    mode: ToolSearchMode
    searched: Mapping[str, ToolDiscoveryDefinition]
    direct_tools: Mapping[str, Tool]
    definitions: tuple[ModelToolDefinition, ...]

    @classmethod
    def build(
        cls,
        catalog: ToolCatalogSnapshot,
        mode: ToolSearchMode,
        discoveries: Mapping[str, ToolDiscoveryDefinition],
    ) -> ToolExposureSnapshot:
        valid: dict[str, ToolDiscoveryDefinition] = {}
        for name, record in discoveries.items():
            tool = catalog.get(name)
            if (
                tool is not None
                and tool.exposure is ToolExposure.SEARCHABLE
                and definition_fingerprint(tool.definition) == record.fingerprint
            ):
                valid[name] = record

        eager = sorted(
            (tool for tool in catalog.tools if tool.exposure is ToolExposure.EAGER),
            key=lambda tool: tool.definition.name,
        )
        searched_tools = sorted(
            (catalog.get(name) for name in valid),
            key=lambda tool: tool.definition.name if tool is not None else "",
        )
        searched_tools = [tool for tool in searched_tools if tool is not None]
        direct = list(eager)
        if mode is ToolSearchMode.NATIVE:
            direct.extend(searched_tools)
        direct_by_name = MappingProxyType(
            {tool.definition.name: tool for tool in direct}
        )
        return cls(
            catalog,
            mode,
            MappingProxyType(valid),
            direct_by_name,
            tuple(tool.definition for tool in direct),
        )

    @property
    def version(self) -> int:
        return self.catalog.version

    def direct(self, name: str) -> Tool | None:
        return self.direct_tools.get(name)

    def target(self, name: str) -> Tool | None:
        return self.catalog.get(name)

    def searched_fingerprints(self) -> Mapping[str, str]:
        return MappingProxyType(
            {name: record.fingerprint for name, record in self.searched.items()}
        )

    def invalidated(
        self, discoveries: Mapping[str, ToolDiscoveryDefinition]
    ) -> tuple[str, ...]:
        return tuple(sorted(set(discoveries) - set(self.searched)))


__all__ = [
    "INVOKE_SEARCHED_TOOL_NAME",
    "TOOL_SEARCH_NAME",
    "ToolExposureSnapshot",
    "definition_fingerprint",
    "discovery_definition",
    "restored_discoveries",
]
