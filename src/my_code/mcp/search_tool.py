"""Local ToolSearch adapter that activates deferred MCP tools for the next step."""

from __future__ import annotations

import json
from typing import Protocol

from my_code.mcp.models import McpSearchMatch, tool_search_name
from my_code.model.primitives import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import Tool, ToolContext, ToolOutput
from my_code.tools.validation import optional_int, required_string


class McpToolSearcher(Protocol):
    async def search_and_activate(
        self,
        server_name: str,
        query: str,
        *,
        limit: int,
    ) -> tuple[McpSearchMatch, ...]: ...


class McpToolSearch(Tool):
    def __init__(
        self,
        searcher: McpToolSearcher,
        *,
        server_name: str,
        tool_count: int,
    ) -> None:
        self._searcher = searcher
        self.server_name = server_name
        self.tool_count = tool_count
        self._definition = ModelToolDefinition(
            name=tool_search_name(server_name),
            description=(
                f"Search {tool_count} deferred tools from MCP server {server_name}. "
                "Matching tools become available starting with the next model step."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Name or capability to search for",
                    },
                    "limit": {
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

    def user_facing_name(self, tool_input: JsonObject) -> str:
        del tool_input
        return f"Search MCP tools: {self.server_name}"

    def get_activity_description(self, tool_input: JsonObject) -> str:
        del tool_input
        return f"Searching MCP tools from {self.server_name}"

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="Searching the local MCP tool index is allowed.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "local-mcp-index"
            ),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        required_string(tool_input, "query")
        optional_int(tool_input, "limit", 5, minimum=1, maximum=20)
        unexpected = set(tool_input) - {"query", "limit"}
        if unexpected:
            raise ValueError(f"Unexpected input field: {sorted(unexpected)[0]}")

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del context
        query = required_string(tool_input, "query")
        limit = optional_int(tool_input, "limit", 5, minimum=1, maximum=20)
        matches = await self._searcher.search_and_activate(
            self.server_name,
            query,
            limit=limit,
        )
        return ToolOutput(
            json.dumps(
                {
                    "server": self.server_name,
                    "matches": [
                        {
                            "name": match.public_name,
                            "remoteName": match.remote_name,
                            "description": match.description,
                        }
                        for match in matches
                    ],
                    "availableFrom": "next_step",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={"match_count": len(matches)},
        )


__all__ = [
    "McpToolSearch",
    "McpToolSearcher",
]
