"""Global provider-neutral ToolSearch and searched-tool dispatcher protocol."""

from __future__ import annotations

import json

from my_code.conversation.attachments import ToolDiscoveryAttachment
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolOutput,
)
from my_code.tools.discovery import (
    INVOKE_SEARCHED_TOOL_NAME,
    TOOL_SEARCH_NAME,
    discovery_definition,
)
from my_code.tools.validation import optional_int, required_string


class ToolSearch(Tool):
    def __init__(self, mode: ToolSearchMode) -> None:
        self.mode = mode
        route = (
            " In dispatcher mode, call matches only through InvokeSearchedTool."
            if mode is ToolSearchMode.DISPATCHER
            else " In native mode, call matches directly starting next step."
        )
        self._definition = ModelToolDefinition(
            TOOL_SEARCH_NAME,
            "Find hidden tools by name or description. Results are available next "
            f"step.{route} Use select:ToolA,ToolB for exact selection.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return self._definition

    def is_concurrency_safe(self, tool_input: JsonObject) -> bool:
        del tool_input
        return True

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Searching the local tool index is allowed.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "local-tool-index"
            ),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "query")
        optional_int(tool_input, "max_results", 5, minimum=1, maximum=20)
        unexpected = set(tool_input) - {"query", "max_results"}
        if unexpected:
            raise ValueError(f"Unexpected input field: {sorted(unexpected)[0]}")

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        query = required_string(tool_input, "query").strip()
        limit = optional_int(tool_input, "max_results", 5, minimum=1, maximum=20)
        searchable = tuple(
            tool
            for tool in context.available_tools.values()
            if tool.exposure.value == "searchable"
        )
        matches = _search(searchable, query)[:limit]
        definitions = tuple(discovery_definition(tool) for tool in matches)
        new_definitions = tuple(
            item
            for item in definitions
            if context.searched_fingerprints.get(item.name) != item.fingerprint
        )
        attachments = (
            (ToolDiscoveryAttachment(new_definitions, self.mode.value),)
            if new_definitions
            else ()
        )
        return ToolOutput(
            json.dumps(
                {
                    "query": query,
                    "matches": [item.name for item in definitions],
                    "total": len(definitions),
                    "available_from": "next_step",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={"match_count": len(definitions)},
            new_attachments=attachments,
        )


def _search(tools: tuple[Tool, ...], query: str) -> tuple[Tool, ...]:
    if query.casefold().startswith("select:"):
        selected = {
            name.strip() for name in query.split(":", 1)[1].split(",") if name.strip()
        }
        return tuple(
            sorted(
                (tool for tool in tools if tool.definition.name in selected),
                key=lambda tool: tool.definition.name,
            )
        )
    terms = tuple(term for term in query.casefold().split() if term)
    ranked: list[tuple[int, str, Tool]] = []
    for tool in tools:
        name = tool.definition.name.casefold()
        description = tool.definition.description.casefold()
        if terms and not all(term in name or term in description for term in terms):
            continue
        score = sum(4 if term in name else 1 for term in terms)
        ranked.append((-score, tool.definition.name, tool))
    return tuple(item[2] for item in sorted(ranked))


class InvokeSearchedTool(Tool):
    def __init__(self) -> None:
        self._definition = ModelToolDefinition(
            INVOKE_SEARCHED_TOOL_NAME,
            "Call a tool found by ToolSearch. Never call searched tools directly. "
            "Use the exact tool_name and schema-valid arguments.",
            {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "minLength": 1},
                    "arguments": {"type": "object"},
                },
                "required": ["tool_name", "arguments"],
                "additionalProperties": False,
            },
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return self._definition

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "tool_name")
        if set(tool_input) != {"tool_name", "arguments"}:
            raise ValueError("Expected exactly tool_name and arguments")
        if not isinstance(tool_input.get("arguments"), dict):
            raise ValueError("arguments must be an object")

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Dispatcher routing is validated before target permissions.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "searched-tool-dispatch"
            ),
        )

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        del tool_input, context
        raise ToolExecutionError("InvokeSearchedTool must be handled by ToolExecutor")


__all__ = ["InvokeSearchedTool", "ToolSearch"]
