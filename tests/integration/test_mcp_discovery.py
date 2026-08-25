"""MCP-03: refresh and deferred activation publish only future snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from my_code.conversation.models import ToolCall
from my_code.foundation.json import JsonObject
from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpConnectionState,
    McpDiagnosticCode,
    McpRemoteTool,
    McpServerSpec,
)
from my_code.mcp.runtime import McpRuntime
from my_code.mcp.transport import McpTransport
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.catalog import ToolCatalog
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


class DiscoveryTransport:
    def __init__(self, tools: tuple[McpRemoteTool, ...]) -> None:
        self.tools = tools
        self.handler: Callable[[], None] | None = None
        self.closed = False
        self.block_next_list = False
        self.list_started = asyncio.Event()
        self.list_release = asyncio.Event()

    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None:
        self.handler = handler

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
        del timeout_seconds
        return McpConnectionInfo("2025-11-25", "discovery-fake", "1.0")

    async def list_tools(self, *, timeout_seconds: float) -> tuple[McpRemoteTool, ...]:
        del timeout_seconds
        result = self.tools
        if self.block_next_list:
            self.block_next_list = False
            self.list_started.set()
            await self.list_release.wait()
        return result

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> McpCallResult:
        del timeout_seconds
        return McpCallResult(f"{name}:{arguments}")

    async def close(self) -> None:
        self.closed = True

    def emit_tools_changed(self) -> None:
        assert self.handler is not None
        self.handler()


def tool(name: str, description: str = "original") -> McpRemoteTool:
    return McpRemoteTool(
        name,
        description,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def runtime(
    tmp_path: Path,
    transport: DiscoveryTransport,
    *,
    threshold: int = 50,
) -> tuple[McpRuntime, ToolCatalog]:
    catalog = ToolCatalog()

    def factory(spec: McpServerSpec) -> McpTransport:
        del spec
        return transport

    return (
        McpRuntime(
            enabled=True,
            servers=(McpServerSpec("remote", "fake", tmp_path),),
            catalog=catalog,
            transport_factory=factory,
            deferred_tool_threshold=threshold,
        ),
        catalog,
    )


@pytest.mark.asyncio
async def test_explicit_refresh_add_update_remove_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport((tool("lookup"),))
    mcp, catalog = runtime(tmp_path, transport)
    await mcp.start()
    current_step = catalog.snapshot()
    initial_version = current_step.version

    transport.tools = (tool("lookup", "updated"), tool("create"))
    refreshed = await mcp.refresh("remote")

    next_step = catalog.snapshot()
    assert refreshed.state is McpConnectionState.CONNECTED
    assert next_step.version == initial_version + 1
    assert current_step.get("mcp__remote__lookup").definition.description == "original"  # type: ignore[union-attr]
    assert next_step.get("mcp__remote__lookup").definition.description == "updated"  # type: ignore[union-attr]
    assert next_step.get("mcp__remote__create") is not None

    await mcp.refresh("remote")
    assert catalog.version == next_step.version

    transport.tools = (tool("create"),)
    await mcp.refresh("remote")
    after_remove = catalog.snapshot()
    assert after_remove.get("mcp__remote__lookup") is None
    assert current_step.get("mcp__remote__lookup") is not None

    version_before_invalid = catalog.version
    transport.tools = (tool("invalid", description="bad"),)
    transport.tools[0].input_schema["type"] = "string"
    failed = await mcp.refresh("remote")
    assert failed.state is McpConnectionState.CONNECTED
    assert failed.diagnostic is not None
    assert failed.diagnostic.code is McpDiagnosticCode.DISCOVERY_FAILED
    assert catalog.version == version_before_invalid
    assert catalog.snapshot().get("mcp__remote__create") is not None

    await mcp.close()


@pytest.mark.asyncio
async def test_tools_changed_notification_coalesces_into_refresh(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport((tool("before"),))
    mcp, catalog = runtime(tmp_path, transport)
    await mcp.start()
    version = catalog.version
    transport.tools = (tool("after"),)

    transport.emit_tools_changed()
    transport.emit_tools_changed()
    await mcp.wait_for_refreshes()

    assert catalog.version == version + 1
    assert catalog.snapshot().get("mcp__remote__before") is None
    assert catalog.snapshot().get("mcp__remote__after") is not None
    await mcp.close()


@pytest.mark.asyncio
async def test_notification_during_refresh_triggers_one_follow_up_diff(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport((tool("before"),))
    mcp, catalog = runtime(tmp_path, transport)
    await mcp.start()
    version = catalog.version
    transport.tools = (tool("intermediate"),)
    transport.block_next_list = True

    transport.emit_tools_changed()
    await transport.list_started.wait()
    transport.tools = (tool("final"),)
    transport.emit_tools_changed()
    transport.list_release.set()
    await mcp.wait_for_refreshes()

    assert catalog.version == version + 2
    assert catalog.snapshot().get("mcp__remote__final") is not None
    assert catalog.snapshot().get("mcp__remote__intermediate") is None
    await mcp.close()


@pytest.mark.asyncio
async def test_deferred_tool_search_activates_match_for_next_step_only(
    tmp_path: Path,
) -> None:
    transport = DiscoveryTransport(
        (
            tool("weather.lookup", "Get weather forecasts"),
            tool("repository-search", "Search repository text"),
        )
    )
    mcp, catalog = runtime(tmp_path, transport, threshold=1)
    await mcp.start()
    search_step = catalog.snapshot()
    assert search_step.get("mcp_search__remote") is not None
    assert search_step.get("mcp__remote__weather_dot_lookup") is None
    executor = ToolExecutor(
        tools=search_step,
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall("search-1", "mcp_search__remote", {"query": "weather"}),
        tools=search_step,
    )

    assert outcome.result.is_error is False
    assert '"availableFrom":"next_step"' in outcome.result.content
    assert search_step.get("mcp__remote__weather_dot_lookup") is None
    next_step = catalog.snapshot()
    assert next_step.version == search_step.version + 1
    assert next_step.get("mcp__remote__weather_dot_lookup") is not None
    assert next_step.get("mcp__remote__repository-search") is None
    assert next_step.get("mcp_search__remote") is not None

    # Refresh keeps a still-existing activation and updates its definition.
    transport.tools = (
        tool("weather.lookup", "Updated weather forecasts"),
        tool("repository-search", "Search repository text"),
    )
    await mcp.refresh("remote")
    updated = catalog.snapshot().get("mcp__remote__weather_dot_lookup")
    assert updated is not None
    assert updated.definition.description == "Updated weather forecasts"

    await mcp.close()
