"""Standard Tool adapter for one discovered MCP tool."""

from __future__ import annotations

from typing import Protocol

from my_code.foundation.json import JsonObject
from my_code.mcp.models import McpCallResult, McpRemoteTool, public_tool_name
from my_code.mcp.schema import validate_tool_input
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import Tool, ToolExecutionContext, ToolExposure, ToolOutput


class McpToolCaller(Protocol):
    async def call_tool(
        self, server_name: str, remote_name: str, arguments: JsonObject
    ) -> McpCallResult: ...


class McpTool(Tool):
    """Keep MCP execution inside the normal validation and permission pipeline."""

    def __init__(
        self,
        caller: McpToolCaller,
        *,
        server_name: str,
        remote: McpRemoteTool,
    ) -> None:
        self._caller = caller
        self.server_name = server_name
        self.remote = remote
        self._definition = ModelToolDefinition(
            name=public_tool_name(server_name, remote.name),
            description=(
                remote.description.strip()
                or f"MCP tool {remote.name} provided by server {server_name}."
            ),
            input_schema=remote.input_schema,
        )

    @property
    def definition(self) -> ModelToolDefinition:
        return self._definition

    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    def user_facing_name(self, tool_input: JsonObject) -> str:
        del tool_input
        return f"MCP {self.server_name}: {self.remote.name}"

    def get_activity_description(self, tool_input: JsonObject) -> str:
        del tool_input
        return f"Calling MCP server {self.server_name}"

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.passthrough(
            message="Remote MCP tools require the local permission policy.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "remote-mcp-tool"
            ),
            updated_input=tool_input,
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        validate_tool_input(self.remote.input_schema, tool_input)

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        del context
        result = await self._caller.call_tool(
            self.server_name,
            self.remote.name,
            tool_input,
        )
        return ToolOutput(
            content=result.content,
            is_error=result.is_error,
            metadata={
                "mcp_server": self.server_name,
                "mcp_tool": self.remote.name,
            },
        )


__all__ = [
    "McpTool",
    "McpToolCaller",
]
