"""Narrow semantic transport boundary used by MCP runtime and offline fakes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpRemoteTool,
    McpServerSpec,
)
from my_code.model.primitives import JsonObject


class McpTransportError(RuntimeError):
    """A sanitized MCP transport error safe to cross the MCP module boundary."""


class McpConfigurationError(McpTransportError):
    pass


class McpConnectionError(McpTransportError):
    pass


class McpProtocolError(McpTransportError):
    pass


class McpRequestError(McpTransportError):
    def __init__(self, code: int | str = "remote_error") -> None:
        self.code = code
        super().__init__(f"Remote MCP request failed ({code})")


class McpTransport(Protocol):
    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None: ...

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo: ...

    async def list_tools(
        self, *, timeout_seconds: float
    ) -> tuple[McpRemoteTool, ...]: ...

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> McpCallResult: ...

    async def close(self) -> None: ...


class McpTransportFactory(Protocol):
    def __call__(self, spec: McpServerSpec) -> McpTransport: ...


__all__ = [
    "McpConfigurationError",
    "McpConnectionError",
    "McpProtocolError",
    "McpRequestError",
    "McpTransport",
    "McpTransportError",
    "McpTransportFactory",
]
