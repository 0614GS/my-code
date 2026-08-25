"""MCP-01: server lifecycle and catalog publication stay atomic."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from my_code.conversation.models import ToolCall
from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpConnectionState,
    McpDiagnosticCode,
    McpRemoteTool,
    McpServerSpec,
)
from my_code.mcp.runtime import McpRuntime, McpRuntimeError
from my_code.mcp.transport import McpConnectionError, McpTransport
from my_code.model.primitives import JsonObject
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.workspace.local import Workspace


class FakeTransport:
    def __init__(
        self,
        tools: tuple[McpRemoteTool, ...],
        *,
        fail_connect: bool = False,
        fail_call: bool = False,
    ) -> None:
        self.tools = tools
        self.fail_connect = fail_connect
        self.fail_call = fail_call
        self.events: list[str] = []
        self.tools_changed_handler: Callable[[], None] | None = None

    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None:
        self.tools_changed_handler = handler

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
        self.events.append(f"connect:{timeout_seconds}")
        if self.fail_connect:
            raise McpConnectionError("secret startup detail")
        return McpConnectionInfo("2025-11-25", "fake", "1.0")

    async def list_tools(self, *, timeout_seconds: float) -> tuple[McpRemoteTool, ...]:
        self.events.append(f"list:{timeout_seconds}")
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> McpCallResult:
        self.events.append(f"call:{name}:{arguments}:{timeout_seconds}")
        if self.fail_call:
            raise McpConnectionError("secret disconnect detail")
        return McpCallResult(f"called:{name}:{arguments}")

    async def close(self) -> None:
        self.events.append("close")


class FakeFactory:
    def __init__(self, transports: list[FakeTransport]) -> None:
        self.transports = transports
        self.created: list[FakeTransport] = []

    def __call__(self, spec: McpServerSpec) -> McpTransport:
        del spec
        transport = self.transports.pop(0)
        self.created.append(transport)
        return transport


def remote_tool(
    name: str = "lookup",
    schema: JsonObject | None = None,
) -> McpRemoteTool:
    return McpRemoteTool(
        name,
        "Look up a value",
        schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def spec(tmp_path: Path, name: str = "search") -> McpServerSpec:
    return McpServerSpec(
        name,
        "fake-server",
        tmp_path,
        startup_timeout_seconds=3.0,
        call_timeout_seconds=7.0,
    )


@pytest.mark.asyncio
async def test_connect_discover_call_disconnect_reconnect_and_close(
    tmp_path: Path,
) -> None:
    first = FakeTransport((remote_tool(),), fail_call=True)
    second = FakeTransport((remote_tool("find-item"),))
    factory = FakeFactory([first, second])
    catalog = ToolCatalog()
    runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path),),
        catalog=catalog,
        transport_factory=factory,
    )

    await runtime.start()

    connected = runtime.snapshot("search")
    assert connected.state is McpConnectionState.CONNECTED
    assert connected.tool_names == ("mcp__search__lookup",)
    assert catalog.snapshot().get("mcp__search__lookup") is not None
    with pytest.raises(McpRuntimeError, match="disconnected during tool call"):
        await runtime.call_tool("search", "lookup", {"query": "value"})
    assert runtime.snapshot("search").state is McpConnectionState.FAILED
    assert catalog.snapshot().get("mcp__search__lookup") is None
    diagnostic = runtime.snapshot("search").diagnostic
    assert diagnostic is not None
    assert "secret" not in diagnostic.message

    reconnected = await runtime.reconnect("search")

    assert reconnected.state is McpConnectionState.CONNECTED
    assert reconnected.tool_names == ("mcp__search__find-item",)
    assert await runtime.call_tool("search", "find-item", {"query": "again"}) == (
        McpCallResult("called:find-item:{'query': 'again'}")
    )
    await runtime.disconnect("search")
    assert runtime.snapshot("search").state is McpConnectionState.FAILED
    assert catalog.snapshot().get("mcp__search__find-item") is None

    await runtime.close()
    await runtime.close()

    assert runtime.snapshot("search").state is McpConnectionState.CLOSED
    assert first.events[-1] == "close"
    assert second.events[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        (remote_tool(schema={"type": "string"}),),
        (remote_tool("same-name"), remote_tool("same-name")),
    ],
)
async def test_invalid_or_colliding_discovery_does_not_publish_partial_source(
    tmp_path: Path,
    tools: tuple[McpRemoteTool, ...],
) -> None:
    transport = FakeTransport(tools)
    catalog = ToolCatalog()
    catalog_version = catalog.version
    runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path),),
        catalog=catalog,
        transport_factory=FakeFactory([transport]),
    )

    await runtime.start()

    snapshot = runtime.snapshot("search")
    assert snapshot.state is McpConnectionState.FAILED
    assert snapshot.diagnostic is not None
    assert snapshot.diagnostic.code is McpDiagnosticCode.DISCOVERY_FAILED
    assert catalog.version == catalog_version
    assert ToolSourceId("mcp", "search") not in catalog.sources
    assert transport.events[-1] == "close"


@pytest.mark.asyncio
async def test_catalog_name_conflict_preserves_existing_source(tmp_path: Path) -> None:
    catalog = ToolCatalog()
    existing = remote_tool()

    # This test only needs catalog conflict detection; use the discovered MCP adapter
    # itself as a concrete standard Tool to keep the fake focused on transport.

    class UnusedFactory:
        def __call__(self, spec: McpServerSpec) -> McpTransport:
            del spec
            raise AssertionError("disabled empty runtime must not create a transport")

    seed_runtime = McpRuntime(
        enabled=False,
        servers=(),
        catalog=catalog,
        transport_factory=UnusedFactory(),
    )
    from my_code.mcp.tool import McpTool

    catalog.register_source(
        ToolSourceId("test", "existing"),
        (McpTool(seed_runtime, server_name="search", remote=existing),),
    )
    version = catalog.version
    runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path),),
        catalog=catalog,
        transport_factory=FakeFactory([FakeTransport((remote_tool(),))]),
    )

    await runtime.start()

    assert runtime.snapshot("search").state is McpConnectionState.FAILED
    assert catalog.version == version
    assert catalog.snapshot().source_for("mcp__search__lookup") == ToolSourceId(
        "test", "existing"
    )


@pytest.mark.asyncio
async def test_start_failure_and_cancellation_close_unpublished_transports(
    tmp_path: Path,
) -> None:
    failed = FakeTransport((remote_tool(),), fail_connect=True)
    failed_runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path, "failed"),),
        catalog=ToolCatalog(),
        transport_factory=FakeFactory([failed]),
    )

    await failed_runtime.start()

    failed_snapshot = failed_runtime.snapshot("failed")
    assert failed_snapshot.state is McpConnectionState.FAILED
    assert failed_snapshot.diagnostic is not None
    assert failed_snapshot.diagnostic.code is McpDiagnosticCode.START_FAILED
    assert "secret" not in failed_snapshot.diagnostic.message
    assert failed.events[-1] == "close"

    class BlockingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__((remote_tool(),))
            self.started = asyncio.Event()

        async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
            del timeout_seconds
            self.events.append("connect")
            self.started.set()
            await asyncio.Future[None]()
            raise AssertionError("unreachable")

    blocked = BlockingTransport()
    blocked_runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path, "blocked"),),
        catalog=ToolCatalog(),
        transport_factory=FakeFactory([blocked]),
    )
    startup = asyncio.create_task(blocked_runtime.start())
    await blocked.started.wait()

    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert blocked.events[-1] == "close"
    assert blocked_runtime.snapshot("blocked").state is McpConnectionState.FAILED
    await blocked_runtime.close()


@pytest.mark.asyncio
async def test_tool_from_existing_snapshot_reports_unavailable_after_disconnect(
    tmp_path: Path,
) -> None:
    catalog = ToolCatalog()
    runtime = McpRuntime(
        enabled=True,
        servers=(spec(tmp_path),),
        catalog=catalog,
        transport_factory=FakeFactory([FakeTransport((remote_tool(),))]),
    )
    await runtime.start()
    captured = catalog.snapshot()
    await runtime.disconnect("search")
    executor = ToolExecutor(
        tools=captured,
        policy=PermissionPolicy(PermissionMode.BYPASS),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall("call-1", "mcp__search__lookup", {"query": "value"}),
        tools=captured,
    )

    assert outcome.result.is_error is True
    assert outcome.result.content == (
        "McpRuntimeError: MCP server 'search' is unavailable"
    )
